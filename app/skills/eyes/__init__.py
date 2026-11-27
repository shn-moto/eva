"""Skill "глаза": the home's cameras (services/eyes on the hub).

The eyes service watches the cameras itself (motion, faces, who it is by photolib's
face base) and keeps a journal; this gives her read access to it, not a call per
question: the live state with the journal of any period (eyes), and the frame
itself to look at (eyes_look).

On only when the eyes have cameras at the brain's start (2026-11-27, user: «если
есть активные глаза»): a home without them pays no prompt tokens for the tools."""
import base64
import json
import urllib.parse
import urllib.request

from core import profile, skills
from core.skills import Result, Skill, Tool
from core.util import log

API = profile.service("eyes")


def _get(path, timeout=15):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return r.read()


def _cams():
    return json.loads(_get("/cameras", 5)).get("cameras", [])


def state(args, person):
    q = {k: args[k] for k in ("since", "until", "camera", "person") if args.get(k)}
    q["hours"] = args.get("hours") or 2
    j = json.loads(_get("/journal?" + urllib.parse.urlencode(q)))
    now = []
    for c in _cams():
        if not c["online"]:
            now.append({"camera": c["name"], "online": False})
            continue
        ev = c.get("event")
        now.append({"camera": c["name"], "online": True, "motion_now": bool(ev), "since": ev and ev["since"][11:19],
                    "recognised_now": ev and ev["people"], "faces_in_frame": len(c.get("faces") or []),
                    "last_motion": (c.get("last_motion") or "")[:19].replace("T", " ") or None})
    return json.dumps({"now": now, "journal": j}, ensure_ascii=False, separators=(",", ":"))


def look(args, person):
    cams = _cams()
    want = (args.get("camera") or "").lower()
    c = next((c for c in cams if want in (c["id"].lower(), c["name"].lower())), None) or \
        next((c for c in cams if want and want in c["name"].lower()), None) or (cams[0] if len(cams) == 1 else None)
    if not c:
        return "Какая камера? Есть: " + ", ".join(x["name"] for x in cams)
    if not c["online"]:
        return f"Камера «{c['name']}» сейчас не на связи."
    jpg = _get(f"/frame/{c['id']}.jpg?boxes=0")
    data = base64.b64encode(jpg).decode("ascii")
    skills.HISTORY_IMAGES[data] = data        # a camera frame is small already (~50 KB)
    return Result("", blocks=[
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": f"Кадр камеры «{c['name']}» сейчас."}])


try:
    _n = len(_cams())
except Exception as e:      # the eyes service away at start: no skill, no tokens
    log(f"eyes: not reachable ({e}) - skill off")
    _n = 0

if _n:
    SKILL = Skill(
        name="eyes",
        summary="глаза: камеры дома - движение, лица, кто где был",
        tools=[Tool(
            name="eyes",
            description=("The home's cameras: now (motion, faces in frame, who is recognised) plus the journal of "
                         "motion events with the people recognised by face (similarity 0-1; 0.5+ is a match). "
                         "Default: the last 2 hours; hours or since/until (ISO) for another period, camera and "
                         "person to narrow. A camera sees one room only: for who is home, weigh it with the voices."),
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"hours": {"type": "number"}, "since": {"type": "string"},
                                   "until": {"type": "string"}, "camera": {"type": "string"},
                                   "person": {"type": "string"}}},
            handler=state, needs="known",
        ), Tool(
            name="eyes_look",
            description="A camera's frame now, to look at yourself. camera - its name.",
            schema={"type": "object", "additionalProperties": False, "properties": {"camera": {"type": "string"}}},
            handler=look, needs="adult",
        )],
    )
