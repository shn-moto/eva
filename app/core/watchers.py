"""Camera scenarios: rules over what the eyes see (services/eyes, its /events stream), the same
actions as the listeners (core/listeners.py _act: say, telegram, music, eva, log). Edited on
the admin page «Глаза», kept in

    /var/lib/eva/watchers.json   {"watchers": [...]}

A scenario:

    {"id", "name", "enabled",
     "camera":   "any" | a camera id (eyes.json)
     "on":       "motion"  - motion began (an event opens)
                 "person"  - a face recognised (photolib) - once an event
                 "unknown" - faces not recognised: `unknown_count` photolib answers with no name in
                             one event, and nobody recognised in it
                 "quiet"   - no motion for `quiet_min` minutes after there was some (once, until
                             the next motion)
     "who":      "any" | a person's name (photolib), for "person"
     "unknown_count": 2,  "quiet_min": 10,
     "hours":    "" | "22:00-07:00" - only within this time of day
     "action", "text", "to", "track", "trackUrl", "cooldown"  - as a listener's}

{camera} (its name), {who}, {time} in "text" are filled in. Every firing ends in a line
"watcher <id>: ok: ..." / "error: ..." (eva_log) - the admin page shows them.
"""
import json
import os
import threading
import time
import urllib.request

from core import listeners, profile
from core.util import log

FILE = os.environ.get("EVA_WATCHERS", "/var/lib/eva/watchers.json")
URL = profile.service("eyes") + "/events"
_state = {"mtime": -1, "items": [], "fired": {}}
_events = {}              # event_id -> {"camera", "named": set, "unknown": n, "fired": set(watcher ids)}
_cams = {}                # camera id -> {"place", "open": bool, "last": ts, "armed": set(watcher ids)}
_lock = threading.Lock()


def load():
    try:
        m = os.path.getmtime(FILE)
    except OSError:
        m = None
    if m != _state["mtime"]:
        _state["mtime"] = m
        if m is None:                           # none made on the page yet
            _state["items"] = []
            return _state["items"]
        try:
            with open(FILE, encoding="utf-8") as f:
                _state["items"] = json.load(f).get("watchers", [])
            log(f"watchers: {len([x for x in _state['items'] if x.get('enabled')])} on")
        except (OSError, ValueError) as e:
            log(f"watchers: bad file, kept the old ones: {e}")
    return _state["items"]


def _in_hours(spec, now=None):
    """'22:00-07:00' (over midnight too); empty - always."""
    if not spec or "-" not in spec:
        return True
    try:
        a, b = [int(p.split(":")[0]) * 60 + int((p.split(":") + ["0"])[1]) for p in spec.replace(" ", "").split("-")]
    except ValueError:
        return True
    t = time.localtime(now)
    m = t.tm_hour * 60 + t.tm_min
    return a <= m < b if a <= b else (m >= a or m < b)


def _fire(x, ev, who, desc):
    if not _in_hours(x.get("hours")):
        return
    if time.time() - _state["fired"].get(x["id"], 0) < float(x.get("cooldown") or 0):
        return
    _state["fired"][x["id"]] = time.time()
    place = ev.get("place") or ev.get("camera")
    extra = {"camera": place, "time": time.strftime("%H:%M"), "who": who}
    ctx = (f"Камера «{place}», {time.strftime('%H:%M')}. Что видно сейчас - инструмент eyes_look, "
           f"журнал камер - eyes.")
    try:
        listeners._act(x, time.time(), desc, who, [], extra=extra, kind="watcher", context=ctx)
    except Exception as e:
        log(f"watcher {x['id']}: error: {e!r}")


def _mine(x, cam):
    return x.get("enabled") and x.get("camera", "any") in ("any", "", cam)


def on_event(ev):
    t, cam = ev.get("type"), ev.get("camera")
    place = ev.get("place") or cam
    eid = ev.get("event_id")
    with _lock:
        c = _cams.setdefault(cam, {"place": place, "open": False, "last": 0, "armed": set()})
        c["place"] = place
        e = _events.setdefault(eid, {"camera": cam, "named": set(), "unknown": 0, "fired": set(), "ts": time.time()}) \
            if eid is not None else None
        for k in [k for k, v in _events.items() if time.time() - v["ts"] > 3600]:
            del _events[k]
    items = [x for x in load() if _mine(x, cam)]
    if t == "motion":
        c["open"], c["last"] = True, time.time()
        for x in items:
            if x.get("on") == "quiet":
                c["armed"].add(x["id"])        # after this motion, its quiet counts
            elif x.get("on") == "motion":
                _fire(x, ev, f"камера «{place}»", f"движение: {place}")
    elif t == "end":
        c["open"], c["last"] = False, time.time()
    elif t == "face" and e is not None:
        c["last"] = time.time()
        who = ev.get("person")
        if who:
            e["named"].add(who)
        else:
            e["unknown"] += 1
        for x in items:
            if x["id"] in e["fired"]:
                continue
            on = x.get("on")
            if on == "person" and who:
                want = (x.get("who") or "any").strip().lower()
                if want in ("", "any") or want in who.lower():
                    e["fired"].add(x["id"])
                    _fire(x, ev, who, f"{who} в кадре: {place} (сходство {ev.get('similarity')})")
            elif on == "unknown" and not who and not e["named"] and e["unknown"] >= int(x.get("unknown_count") or 2):
                e["fired"].add(x["id"])
                _fire(x, ev, "незнакомое лицо", f"незнакомое лицо: {place}")


def _quiet():
    while True:
        time.sleep(15)
        try:
            items = {x["id"]: x for x in load() if x.get("enabled") and x.get("on") == "quiet"}
            for cam, c in list(_cams.items()):
                for xid in list(c["armed"]):
                    x = items.get(xid)
                    if not x or not _mine(x, cam):
                        c["armed"].discard(xid)
                        continue
                    if not c["open"] and time.time() - c["last"] >= 60 * float(x.get("quiet_min") or 10):
                        c["armed"].discard(xid)
                        _fire(x, {"camera": cam, "place": c["place"]}, f"камера «{c['place']}»",
                              f"тихо {float(x.get('quiet_min') or 10):g} мин: {c['place']}")
        except Exception as e:
            log(f"watchers: quiet: {e!r}")


def _listen():
    fails = 0
    while True:
        try:
            with urllib.request.urlopen(URL, timeout=40) as r:
                log("watchers: eyes connected")
                fails = 0
                for line in r:
                    line = line.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        try:
                            on_event(json.loads(line[5:]))
                        except Exception as e:
                            log(f"watchers: event: {e!r}")
        except Exception as e:
            if fails == 0:
                log(f"watchers: eyes stream: {e}")
            fails += 1
        time.sleep(min(60, 3 * (fails + 1)))


def start():
    load()
    threading.Thread(target=_listen, daemon=True).start()
    threading.Thread(target=_quiet, daemon=True).start()
