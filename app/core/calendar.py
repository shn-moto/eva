"""Eva's calendar and scheduler: what she was asked to do at a time - remind,
play music, wake with an alarm, carry out an instruction - once or on a repeat,
and the family's birthdays from photolib's tree.

    /var/lib/eva/calendar.json   {"events": [...], "said": {...}}

An event:
    {"id", "title", "next": "YYYY-MM-DDTHH:MM", "repeat": None | {"every": day|weekdays|
     weekend|week|month|year, "days": [0..6] (week: Mon=0)}, "action": remind|music|alarm|task,
     "text", "query", "notify": home|telegram|both, "who": the person it is for,
     "by": who asked, "by_profile", "channel", "created"}

The scheduler (a thread in the brain) looks every few seconds; a due event is
done by the handler of its action (skills/calendar sets HANDLERS - the skill
knows the voice, music, Telegram) and moved to its next time, or removed.
An event missed by more than GRACE (the brain was down) is not done late -
a 6:00 alarm at 9:00 helps nobody - it is logged and moved on.
"""
import calendar as _cal
import datetime as dt
import json
import os
import threading
import time
import uuid

from core import profile
from core.util import log

FILE = os.environ.get("EVA_CALENDAR", "/var/lib/eva/calendar.json")
GRACE = profile.tune("calendar.grace", 900)                      # s: later than this a missed event is skipped
TICK = profile.tune("calendar.tick", 5)
BIRTHDAY_TIME = profile.tune("calendar.birthday_time", "09:00")    # today's birthdays, aloud at home
BIRTHDAY_EVE_TIME = profile.tune("calendar.birthday_eve_time", "20:00")   # tomorrow's, the evening before
EVERY = ("day", "weekdays", "weekend", "week", "month", "year")

HANDLERS = {}            # action -> fn(event); set by skills/calendar
BIRTHDAY = [None]        # fn(text) that says a birthday line; set by skills/calendar
_lock = threading.Lock()


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("events", [])
    d.setdefault("said", {})
    return d


def _save(d):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def parse(when):
    """'YYYY-MM-DD HH:MM' (or with T) -> datetime, local time."""
    return dt.datetime.fromisoformat(str(when).strip().replace(" ", "T")[:16])


def fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M")


def _add_months(t, n):
    m = t.month - 1 + n
    y, m = t.year + m // 12, m % 12 + 1
    return t.replace(year=y, month=m, day=min(t.day, _cal.monthrange(y, m)[1]))


def following(t, repeat):
    """The next time after t for a repeat rule (t itself is the one just done)."""
    every = repeat.get("every")
    if every == "month":
        return _add_months(t, 1)
    if every == "year":
        try:
            return t.replace(year=t.year + 1)
        except ValueError:                    # 29 February
            return t.replace(year=t.year + 1, day=28)
    days = {"day": range(7), "weekdays": range(5), "weekend": (5, 6)}.get(every)
    if every == "week":
        days = repeat.get("days") or [t.weekday()]
    nxt = t + dt.timedelta(days=1)
    while nxt.weekday() not in days:
        nxt += dt.timedelta(days=1)
    return nxt


def first(when, repeat):
    """The first time of a new event: `when`, moved onto an allowed day for a repeat."""
    t = parse(when)
    if repeat and repeat.get("every") in ("weekdays", "weekend", "week"):
        days = {"weekdays": range(5), "weekend": (5, 6)}.get(repeat["every"]) or repeat.get("days") or [t.weekday()]
        while t.weekday() not in days:
            t += dt.timedelta(days=1)
    return t


def add(ev):
    """A new event (validated by the skill). -> the stored event"""
    repeat = ev.get("repeat") or None
    if repeat and repeat.get("every") not in EVERY:
        raise ValueError(f"repeat.every: {', '.join(EVERY)}")
    t = first(ev["when"], repeat)
    minute = dt.datetime.now().replace(second=0, microsecond=0)     # "now" to the minute: 7:00 at 7:00 is today
    if t < minute and not repeat:
        raise ValueError(f"время уже прошло: {t:%d.%m.%Y %H:%M}")
    while repeat and t < minute:
        t = following(t, repeat)
    e = {k: v for k, v in ev.items() if k != "when"}
    e.update(id=uuid.uuid4().hex[:6], next=fmt(t), repeat=repeat, created=fmt(dt.datetime.now()))
    with _lock:
        d = _load()
        d["events"].append(e)
        _save(d)
    log(f"calendar: added {e['id']} {e['action']} «{e.get('title')}» at {e['next']} repeat {repeat}")
    return e


def remove(eid):
    with _lock:
        d = _load()
        kept = [e for e in d["events"] if e["id"] != eid]
        if len(kept) == len(d["events"]):
            return None
        gone = next(e for e in d["events"] if e["id"] == eid)
        d["events"] = kept
        _save(d)
    log(f"calendar: removed {eid} «{gone.get('title')}»")
    return gone


def events():
    with _lock:
        return sorted(_load()["events"], key=lambda e: e["next"])


def upcoming(days=7):
    """Occurrences in the next `days` days, repeats unrolled. -> [(datetime, event)]"""
    end = dt.datetime.now() + dt.timedelta(days=days)
    out = []
    for e in events():
        t = parse(e["next"])
        n = 0
        while t <= end and n < 60:
            out.append((t, e))
            if not e.get("repeat"):
                break
            t, n = following(t, e["repeat"]), n + 1
    return sorted(out, key=lambda x: x[0])


def _fire(e):
    fn = HANDLERS.get(e["action"])
    if not fn:
        log(f"calendar: no handler for {e['action']} ({e['id']})")
        return
    try:
        fn(e)
    except Exception as ex:
        log(f"calendar: {e['id']} {e['action']} failed: {ex}")


def _birthdays(d, now):
    """Today's birthdays in the morning, tomorrow's the evening before - once each."""
    if not BIRTHDAY[0]:
        return
    hm = now.strftime("%H:%M")
    for key, at, which in (("today", BIRTHDAY_TIME, "сегодня"), ("eve", BIRTHDAY_EVE_TIME, "завтра")):
        stamp = now.strftime("%Y-%m-%d")
        if hm < at or d["said"].get(key) == stamp:
            continue
        if (now - now.replace(hour=int(at[:2]), minute=int(at[3:5]))).total_seconds() > GRACE:
            d["said"][key] = stamp            # the brain was down at that time: not hours late
            continue
        d["said"][key] = stamp
        try:
            import photolib
            lines = [x.split(": ", 1)[1] for x in photolib.birthdays(days=1) if x.startswith(which + ":")]
        except Exception as ex:
            log(f"calendar: birthdays: {ex}")
            continue
        if lines:
            BIRTHDAY[0](which, lines)


def run():
    while True:
        time.sleep(TICK)
        now = dt.datetime.now()
        due = []
        with _lock:
            d = _load()
            changed = False
            for e in list(d["events"]):
                t = parse(e["next"])
                if t > now:
                    continue
                late = (now - t).total_seconds()
                if late <= GRACE:
                    due.append(dict(e))
                else:
                    log(f"calendar: {e['id']} «{e.get('title')}» missed by {late / 60:.0f} min - skipped")
                if e.get("repeat"):
                    while t <= now:
                        t = following(t, e["repeat"])
                    e["next"] = fmt(t)
                else:
                    d["events"].remove(e)
                changed = True
            said = dict(d["said"])
            _birthdays(d, now)
            if changed or said != d["said"]:
                _save(d)
        for e in due:
            log(f"calendar: {e['id']} {e['action']} «{e.get('title')}» now")
            threading.Thread(target=_fire, args=(e,), daemon=True).start()


def start():
    threading.Thread(target=run, daemon=True).start()
