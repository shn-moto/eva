"""Idle slideshow on the TV: when nobody needs Eva, family photos.

First "on this day" - the same date in earlier years (photolib
/memories/on-this-day), oldest year first; then what fits the day: the photos
of whoever has a birthday soon, and ~20 themes of the day (season, holidays)
that the local model on the hub picks once a day - no Claude, no cost; then the
whole archive newest first (photolib /memories/feed), endless.

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
THEMES = profile.tune("slideshow.themes", 20)                # themes of the day from the local model
PER_THEME = profile.tune("slideshow.per_theme", 16)          # 20 x 16 x 12 s: over an hour before the archive
BIRTHDAY_DAYS = profile.tune("slideshow.birthday_days", 3)   # birthdays within this many days get their photos
FEED_GAP = profile.tune("slideshow.feed_gap_min", 30)        # min between archive photos (bursts thinned)
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

state = {"on": False, "playlist": None, "current": None, "held": None}
# When she is called, the slide stays on the TV while she answers: the question
# may be about it («что на этом фото?» - the slideshow went back to the face at
# once and she talked about a photo nobody saw any more, 2026-11-25). A photo
# tool on it pins it as a photo view (skills/photos); otherwise the face comes
# back once she has answered.
HOLD_AFTER = profile.tune("slideshow.hold_after", 3.0)      # s after her answer: then the face
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


def themes(today):
    """Themes of the day from the local model (the router's server): season,
    holidays near this date, things people do now. No names: a theme is found
    by what is in the picture (CLIP), so a theme named after one child's
    birthday showed another child's birthday (2026-11-23). -> [{"title", "query"}]"""
    body = {"messages": [
        {"role": "system", "content": "Ты подбираешь темы для показа семейных фото на телевизоре весь день. "
                                      "Ответ - JSON."},
        {"role": "user", "content": (
            f"Сегодня {today.day} {MONTHS[today.month - 1]} {today.year}, {WEEKDAYS[today.weekday()]}, "
            # the season said outright: from the date alone it gave «Лето в доме» in late November (2026-11-23)
            f"сейчас {SEASONS[(today.month % 12) // 3]} - темы под это время года, не другое. "
            f"Предложи {THEMES} разных тем фото, которые приятно вспомнить сегодня: погода и природа "
            "этого сезона, праздники рядом с этой датой, прогулки, поездки, еда, игры, дом, спорт - чтобы "
            "темы не повторяли друг друга. Без имён людей. title - коротко по-русски (2-4 слова), "
            "query - что видно на фото, по-английски для поиска CLIP (5-10 слов).")}],
        "temperature": 0.8, "max_tokens": 1500,
        "response_format": {"type": "json_object", "schema": {
            "type": "object", "required": ["themes"],
            "properties": {"themes": {"type": "array", "items": {
                "type": "object", "required": ["title", "query"],
                "properties": {"title": {"type": "string"}, "query": {"type": "string"}}}}}}}}
    req = urllib.request.Request(profile.service("router"), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(json.loads(r.read())["choices"][0]["message"]["content"]).get("themes", [])
    seen, keep = set(), []
    for t in out:
        key = (t.get("title") or "").strip().lower()
        if key and t.get("query") and key not in seen:
            seen.add(key)
            keep.append(t)
    return keep[:THEMES]


def theme_photos(theme):
    photos, _ = photolib.search(theme["query"], want=60)
    good = fit([p["image_id"] for p in photos])
    photos = [p for p in photos if p["image_id"] in good]
    random.shuffle(photos)
    out = []
    for p in photolib.spread(photos, PER_THEME):
        # who is really on this photo (faces), not who the theme was about
        try:
            people = photolib.photo_people(p["image_id"])
        except Exception:
            people = []
        out.append({"id": p["image_id"], "date": p.get("photo_date"), "people": people, "title": theme["title"]})
    return out


class Playlist:
    """The day's slideshow, filled as it goes (no waiting before the first
    photo): on this day -> birthdays -> the themes of the day -> the archive
    newest first, endless (the day's selections lasted 9 minutes, there are
    100k photos, 2026-11-23)."""

    AHEAD = 20                     # photos kept ready

    def __init__(self, today):
        self.today, self.items, self.i, self.seen = today, [], 0, set()
        self.filling, self.feed_before = False, None
        self.sources = self._sources()

    def _sources(self):
        yield "on this day", lambda: on_this_day(self.today)
        yield "birthdays", lambda: birthday_photos(self.today)
        try:
            ts = themes(self.today)
        except Exception as e:
            log("slideshow: themes:", e)
            ts = []
        log(f"slideshow: {len(ts)} themes: {', '.join(t['title'] for t in ts)}")
        for t in ts:
            yield f"theme {t['title']!r} ({t['query']})", (lambda t=t: theme_photos(t))
        while True:
            yield "archive", self._feed

    def _feed(self):
        q = {"limit": 60, "min_gap": FEED_GAP, "exclude_tags": ",".join(EXCLUDE_TAGS),
             "require_gps": str(REQUIRE_GPS).lower()}
        if self.feed_before:
            q["before"] = self.feed_before
        d = photolib._get("/memories/feed?" + urllib.parse.urlencode(q))
        photos = d.get("photos", [])
        if not photos:                 # past the oldest photo: from the newest again
            self.feed_before, self.seen = None, set()
            return []
        last = datetime.datetime.fromisoformat(photos[-1]["photo_date"][:19])
        # the next page starts a gap before the last photo: no burst across pages
        self.feed_before = (last - datetime.timedelta(minutes=FEED_GAP)).isoformat()
        return [{"id": p["image_id"], "date": p["photo_date"], "people": p.get("people", []),
                 "title": "Из архива"} for p in photos]

    def _fill(self):
        try:
            while len(self.items) - self.i < self.AHEAD:
                name, make = next(self.sources)
                try:
                    got = [x for x in make() if x["id"] not in self.seen]
                except Exception as e:
                    log(f"slideshow: {name}: {e}")
                    time.sleep(5 if archive_up() else 60)     # a down archive: not a log line every 5 s
                    continue
                self.seen.update(x["id"] for x in got)
                self.items += got
                if name != "archive":
                    log(f"slideshow: {name}: {len(got)} photos")
        finally:
            self.filling = False

    def ensure(self):
        if len(self.items) - self.i < self.AHEAD and not self.filling:
            self.filling = True
            threading.Thread(target=self._fill, daemon=True).start()

    def next(self):
        self.ensure()
        if self.i >= len(self.items):
            return None                # still filling
        item = self.items[self.i]
        self.i += 1
        return item


# -------------------------------------------------------------- the screen --
def describe():
    """For screen.describe(): what the slideshow shows - or showed a moment ago:
    it stops as soon as she is called, so «кто это было?» comes after it - or None."""
    if state["on"] and state["current"]:
        c, lead = state["current"], "На экране заставка из семейных фото"
    elif state.get("held"):
        c, lead = state["held"], "На экране сейчас фото из заставки (стоит, пока ты отвечаешь)"
    elif state.get("last") and time.time() - state["last"][1] < 60:
        c, lead = state["last"][0], "Только что на экране была заставка из семейных фото"
    else:
        return None
    who = f", на фото: {', '.join(c['people'])}" if c.get("people") else ""
    return f"{lead} ({c['title']}): фото id {c['id']}, {_caption(c.get('date'))}{who}."


def _music():
    """The track playing, for a line on the slide: music no longer keeps the
    slideshow away, the slide says what plays instead (Иван, 2026-11-23)."""
    import music
    t = music.PLAYER.now() if music.PLAYER.active() else None
    if not t:
        return None
    return {"title": music.clean(t.get("title")), "artist": music.clean(t.get("artist")),
            "paused": music.PLAYER.paused}


_alive = {"at": 0.0, "ok": True}


def archive_up():
    """photolib answers (checked at most every 10 s). The TV loads each slide from it itself: with the
    archive down the slides queued before kept coming as a broken picture all night (2026-11-28:
    photolib hung in its shutdown at 01:42)."""
    if time.time() - _alive["at"] >= 10:
        try:
            with urllib.request.urlopen(photolib.API + "/health", timeout=3) as r:
                _alive["ok"] = r.status == 200
        except Exception:
            _alive["ok"] = False
        _alive["at"] = time.time()
    return _alive["ok"]


def _show(item):
    state["current"] = item
    ui({"type": "view", "kind": "slide", "id": item["id"], "title": item["title"],
        "caption": _caption(item.get("date"), ", ".join(item.get("people", []))), "music": _music()})


def reshow():
    """The same photo again with the music line as it is now (a new track, the
    music stopped) - instead of the player covering the slideshow."""
    if state["on"] and state["current"]:
        _show(state["current"])
        return True
    return False


def stop(why="", hold=False):
    with _lock:
        if not state["on"]:
            return
        state.update(on=False, current=None, last=(state["current"], time.time()),
                     held=state["current"] if hold else None)
    log(f"slideshow: stop ({why}{', the photo stays while she answers' if hold else ''})")


def held_photo():
    """The slide that stayed on the TV when she was called (or None)."""
    return state.get("held")


def release_held():
    state["held"] = None


QUIET = profile.tune("slideshow.quiet_after_speech", 2.0)    # s after speech before the next photo


def _talking():
    """Someone in the room is speaking now (or just stopped): the next photo waits. The
    slide changed while «измени это фото» was being said and recognised, and she held -
    and redrew - the next one (2026-11-26)."""
    try:
        from core import hub_ear
    except ImportError:
        return False
    st = hub_ear.STATE
    return st.get("active") and (hub_ear.hearing() or time.time() - st.get("at", 0) < QUIET)


def run(busy, last_activity, screen_free, restore=None):
    """The loop (a thread of the brain). busy() - she is wanted right now;
    last_activity() - time of the last conversation / screen use;
    screen_free() - no pictures of a tool on the TV (the music player does not
    count); restore() - what the TV shows when the slideshow stops (the player
    if music plays, else the face)."""
    if not ENABLED:
        log("slideshow: off in the profile")
        return
    shown_at = 0.0
    while True:
        time.sleep(1)
        try:
            idle = not busy() and screen_free() and time.time() - last_activity() >= IDLE_AFTER
            if state["on"] and not idle:
                if busy():
                    stop("she is wanted", hold=True)          # the slide stays: the question may be about it
                    continue
                stop("the screen is used")
                if screen_free():                 # nothing else took the screen: player or face again
                    (restore or (lambda: ui({"type": "view", "kind": "close"})))()
                continue
            if state.get("held"):
                if not screen_free():
                    state["held"] = None          # a tool took the screen (the photo pinned, another view)
                elif not busy() and time.time() - last_activity() >= HOLD_AFTER:
                    state["held"] = None          # she has answered, not about the photo: the face again
                    (restore or (lambda: ui({"type": "view", "kind": "close"})))()
                continue
            if not idle:
                continue
            today = datetime.date.today()
            pl = state.get("playlist")
            if pl is None or pl.today != today:
                pl = state["playlist"] = Playlist(today)
            pl.ensure()
            if time.time() - shown_at < SECONDS:
                continue
            if _talking():
                continue                          # the photo stays while someone speaks: a question may be about it
            if not archive_up():
                if state["on"]:                   # no broken picture: the face (or the player) instead
                    stop("the archive is down")
                    (restore or (lambda: ui({"type": "view", "kind": "close"})))()
                shown_at = time.time()            # look again in SECONDS, not every second
                continue
            item = pl.next()
            if item is None:
                continue                          # the first photos are still being found
            if not state["on"]:
                state["on"] = True
                log("slideshow: start")
            _show(item)
            shown_at = time.time()
        except Exception as e:
            log("slideshow:", e)
            time.sleep(30)
