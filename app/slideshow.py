"""Idle slideshow on the TV: when nobody needs Eva, family photos.

First "on this day" - the same date in earlier years (photolib
/memories/on-this-day), oldest year first; then what fits the day: the photos
of whoever has a birthday soon, and a few themes of the day (season, holidays)
that the local model on the hub picks once a day - no Claude, no cost. Then it
goes round again.

Starts after IDLE_AFTER without a conversation, a picture on the screen or
music; stops at once when she is called, talks, shows something or plays music.
What is shown is known to the screen module, so «кто это?» / «покажи ещё
такие» work on the current photo.
"""
import datetime
import json
import random
import threading
import time
import urllib.parse
import urllib.request

import photolib
from core import profile
from core.speech import ui
from core.util import log

ENABLED = profile.tune("slideshow.enabled", True)
IDLE_AFTER = profile.tune("slideshow.idle_after", 300.0)     # s without a conversation / screen / music
SECONDS = profile.tune("slideshow.seconds", 12.0)            # one photo
PER_YEAR = profile.tune("slideshow.per_year", 4)             # "on this day": photos per year
THEMES = profile.tune("slideshow.themes", 3)                 # themes of the day from the local model
PER_THEME = profile.tune("slideshow.per_theme", 8)
BIRTHDAY_DAYS = profile.tune("slideshow.birthday_days", 3)   # birthdays within this many days get their photos
# never on the TV: photos with these tags (photolib's system tags are out anyway),
# and - with REQUIRE_GPS - photos without real coordinates: none, 0,0 or the
# poles are junk in this archive (screens, scans, downloads)
EXCLUDE_TAGS = profile.tune("slideshow.exclude_tags",
                            ["private", "trash", "document", "документы", "aero", "t1", "body", "mosaic"])
REQUIRE_GPS = profile.tune("slideshow.require_gps", True)

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
SEASONS = ("зима", "весна", "лето", "осень")      # by (month % 12) // 3

state = {"on": False, "list": [], "i": 0, "day": None, "current": None}
_lock = threading.Lock()


# ------------------------------------------------------------ the playlist --
def _caption(photo_date, extra=""):
    try:
        d = datetime.date.fromisoformat(photo_date[:10])
    except (TypeError, ValueError):
        return extra
    years = datetime.date.today().year - d.year
    ago = "" if years <= 0 else f" · {years} {'год' if years % 10 == 1 and years % 100 != 11 else 'года' if 2 <= years % 10 <= 4 and not 12 <= years % 100 <= 14 else 'лет'} назад"
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}{ago}" + (f" · {extra}" if extra else "")


def fit(photo_ids):
    """The ids that may go on the TV: no excluded tag, real coordinates (photolib
    /photo/{id}, in parallel). Search and person photos do not filter by these."""
    from concurrent.futures import ThreadPoolExecutor
    ex = {t.lower() for t in EXCLUDE_TAGS}

    def ok(i):
        try:
            p = photolib.photo(i)
        except Exception:
            return False
        if any(t.get("is_system") or (t.get("name") or "").lower() in ex for t in p.get("tags") or []):
            return False
        return not REQUIRE_GPS or photolib.place(p) is not None

    with ThreadPoolExecutor(8) as pool:
        return {i for i, good in zip(photo_ids, pool.map(ok, photo_ids)) if good}


def on_this_day(today):
    q = urllib.parse.urlencode({"date": today.isoformat(), "per_year": PER_YEAR,
                                "exclude_tags": ",".join(EXCLUDE_TAGS), "require_gps": str(REQUIRE_GPS).lower()})
    d = photolib._get(f"/memories/on-this-day?{q}")
    out = []
    for y in sorted(d.get("years", []), key=lambda y: y["year"]):      # oldest year first
        for p in y["photos"]:
            out.append({"id": p["image_id"], "date": p["photo_date"], "people": p.get("people", []),
                        "title": "В этот день"})
    return out


def birthday_photos(today):
    persons, _ = photolib.tree()
    out = []
    for pid, p in persons.items():
        if not p.get("birth_date") or p.get("death_date") or p.get("birth_date_approx"):
            continue
        try:
            b = datetime.date.fromisoformat(str(p["birth_date"])[:10])
            nxt = b.replace(year=today.year)
        except ValueError:
            continue
        if nxt < today:
            continue
        delta = (nxt - today).days
        if delta > BIRTHDAY_DAYS:
            continue
        when = "сегодня" if delta == 0 else "завтра" if delta == 1 else f"через {delta} дн."
        photos, _ = photolib.person_photos(pid, limit=200)
        cands = photolib.spread(photos, 24)
        good = fit([ph["image_id"] for ph in cands])
        for ph in [ph for ph in cands if ph["image_id"] in good][:8]:
            out.append({"id": ph["image_id"], "date": ph.get("photo_date"), "people": [p["name"]],
                        "title": f"{p['name']}: день рождения {when}"})
    return out


def themes(today, birthdays):
    """Themes of the day from the local model (the router's server): season,
    holidays near this date. -> [{"title", "query"}]"""
    body = {"messages": [
        {"role": "system", "content": "Ты подбираешь темы для показа семейных фото на телевизоре в течение дня. "
                                      "Ответ - JSON."},
        {"role": "user", "content": (
            f"Сегодня {today.day} {MONTHS[today.month - 1]} {today.year}, {WEEKDAYS[today.weekday()]}, "
            # the season said outright: from the date alone it gave «Лето в доме» in late November (2026-11-23)
            f"сейчас {SEASONS[(today.month % 12) // 3]} - темы под это время года, не другое. "
            + (f"Скоро дни рождения: {', '.join(birthdays)}. " if birthdays else "")
            + f"Предложи {THEMES} разные темы фото, которые приятно вспомнить именно сегодня: время года, "
              "погода этого сезона, праздники рядом с этой датой, занятия, которые подходят этому дню. "
              "title - коротко по-русски (2-4 слова), query - описание кадра по-английски для поиска "
              "CLIP (5-10 слов, что видно на фото).")}],
        "temperature": 0.7, "max_tokens": 300,
        "response_format": {"type": "json_object", "schema": {
            "type": "object", "required": ["themes"],
            "properties": {"themes": {"type": "array", "items": {
                "type": "object", "required": ["title", "query"],
                "properties": {"title": {"type": "string"}, "query": {"type": "string"}}}}}}}}
    req = urllib.request.Request(profile.service("router"), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(json.loads(r.read())["choices"][0]["message"]["content"]).get("themes", [])
    return [t for t in out if t.get("title") and t.get("query")][:THEMES]


def theme_photos(theme):
    photos, _ = photolib.search(theme["query"], want=60)
    good = fit([p["image_id"] for p in photos])
    photos = [p for p in photos if p["image_id"] in good]
    random.shuffle(photos)
    return [{"id": p["image_id"], "date": p.get("photo_date"), "people": [], "title": theme["title"]}
            for p in photolib.spread(photos, PER_THEME)]


def build(today):
    """The day's playlist; whatever part fails is left out."""
    items, seen = [], set()
    try:
        bdays = birthday_photos(today)
    except Exception as e:
        log("slideshow: birthdays:", e)
        bdays = []
    parts = [("on this day", lambda: on_this_day(today)), ("birthdays", lambda: bdays)]
    try:
        names = sorted({i["title"].split(":")[0] for i in bdays})
        for t in themes(today, names):
            parts.append((f"theme {t['title']!r} ({t['query']})", lambda t=t: theme_photos(t)))
    except Exception as e:
        log("slideshow: themes:", e)
    for name, make in parts:
        try:
            got = [i for i in make() if i["id"] not in seen]
        except Exception as e:
            log(f"slideshow: {name}: {e}")
            continue
        seen.update(i["id"] for i in got)
        items += got
        log(f"slideshow: {name}: {len(got)} photos")
    return items


# -------------------------------------------------------------- the screen --
def describe():
    """For screen.describe(): what the slideshow shows - or showed a moment ago:
    it stops as soon as she is called, so «кто это было?» comes after it - or None."""
    if state["on"] and state["current"]:
        c, lead = state["current"], "На экране заставка из семейных фото"
    elif state.get("last") and time.time() - state["last"][1] < 60:
        c, lead = state["last"][0], "Только что на экране была заставка из семейных фото"
    else:
        return None
    who = f", на фото: {', '.join(c['people'])}" if c.get("people") else ""
    return f"{lead} ({c['title']}): фото id {c['id']}, {_caption(c.get('date'))}{who}."


def _show(item):
    state["current"] = item
    ui({"type": "view", "kind": "slide", "id": item["id"], "title": item["title"],
        "caption": _caption(item.get("date"), ", ".join(item.get("people", [])))})


def stop(why=""):
    with _lock:
        if not state["on"]:
            return
        state.update(on=False, current=None, last=(state["current"], time.time()))
    log(f"slideshow: stop ({why})")


def run(busy, last_activity, screen_free):
    """The loop (a thread of the brain). busy() - she is wanted right now;
    last_activity() - time of the last conversation / screen / music;
    screen_free() - nothing else on the TV."""
    if not ENABLED:
        log("slideshow: off in the profile")
        return
    shown_at = 0.0
    while True:
        time.sleep(1)
        try:
            idle = not busy() and screen_free() and time.time() - last_activity() >= IDLE_AFTER
            if state["on"] and not idle:
                stop("she is wanted" if busy() else "the screen is used")
                if screen_free():                 # nothing else took the screen: the face comes back
                    ui({"type": "view", "kind": "close"})
                continue
            if not idle:
                continue
            today = datetime.date.today()
            if state["day"] != today or not state["list"]:
                state.update(list=build(today), i=0, day=today)
                if not state["list"]:
                    state["day"] = today            # nothing for today: do not rebuild every second
                    continue
            if not state["on"]:
                state["on"] = True
                log(f"slideshow: start ({len(state['list'])} photos)")
                shown_at = 0.0
            if time.time() - shown_at >= SECONDS:
                item = state["list"][state["i"] % len(state["list"])]
                state["i"] += 1
                _show(item)
                shown_at = time.time()
        except Exception as e:
            log("slideshow:", e)
            time.sleep(30)
