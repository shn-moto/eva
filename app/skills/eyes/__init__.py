"""Skill "глаза": the home's cameras (services/eyes on the hub).

The eyes service watches the cameras itself (motion, faces, who it is by photolib's
face base) and keeps a journal; this gives her read access to it, not a call per
question: the live state with the journal of any period (eyes), and the frame
itself to look at (eyes_look).

Always present; whether the eyes can see is asked at the call - a camera switched off,
motion tracking off, the service down - and she says so. (2026-11-28, user: the skill
existed only when a camera was on at the brain's start, so every switch on the admin page
needed a restart of the brain.)"""
import base64
import json
import urllib.parse
import urllib.request

from core import profile, skills
from core.skills import Result, Skill, Tool

API = profile.service("eyes")
DOWN = "Глаза сейчас недоступны: сервис камер не отвечает."


def _get(path, timeout=15):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return r.read()


def _status():
    """-> the eyes' /cameras ({"cameras", "motion_on", "faces_on"}) or None when the service is away."""
    try:
        return json.loads(_get("/cameras", 5))
    except Exception:
        return None


def _why_blind(c, st):
    """Why a camera sees nothing now, in words she can say; None - it sees."""
    if c.get("off"):
        return "камера выключена на админке"
    if not c.get("online"):
        return f"нет связи с камерой ({c.get('error') or 'нет кадров'})"
    if not c.get("watching", True) and not c.get("faces_enabled", True):
        return "слежение за движением и лицами выключено"
    return None


def state(args, person):
    st = _status()
    if st is None:
        return DOWN
    if not st.get("cameras"):
        return "Камер не подключено."
    now = []
    for c in st["cameras"]:
        why = _why_blind(c, st)
        if why and not c.get("online"):
            now.append({"camera": c["name"], "sees": False, "why": why})
            continue
        ev = c.get("event")
        now.append({"camera": c["name"], "sees": why is None, "why": why,
                    "tracks": {"motion": c.get("watching", True), "faces": c.get("faces_enabled", True)},
                    "motion_now": bool(ev),
                    "since": ev and ev["since"][11:19], "recognised_now": ev and ev["people"],
                    "faces_in_frame": len(c.get("faces") or []),
                    "last_motion": (c.get("last_motion") or "")[:19].replace("T", " ") or None})
    q = {k: args[k] for k in ("since", "until", "camera", "person") if args.get(k)}
    q["hours"] = args.get("hours") or 2
    try:
        j = json.loads(_get("/journal?" + urllib.parse.urlencode(q)))
    except Exception as e:
        j = {"error": f"журнал недоступен: {e}"}
    return json.dumps({"now": now, "faces_on": st.get("faces_on", True), "journal": j},
                      ensure_ascii=False, separators=(",", ":"))


def look(args, person):
    st = _status()
    if st is None:
        return DOWN
    cams = st.get("cameras") or []
    if not cams:
        return "Камер не подключено."
    want = (args.get("camera") or "").lower()
    c = next((c for c in cams if want in (c["id"].lower(), c["name"].lower())), None) or \
        next((c for c in cams if want and want in c["name"].lower()), None) or (cams[0] if len(cams) == 1 else None)
    if not c:
        return "Какая камера? Есть: " + ", ".join(x["name"] for x in cams)
    if c.get("off") or not c.get("online"):
        return f"Камера «{c['name']}»: {_why_blind(c, st)}."
    jpg = _get(f"/frame/{c['id']}.jpg?boxes=0")
    data = base64.b64encode(jpg).decode("ascii")
    skills.HISTORY_IMAGES[data] = data        # a camera frame is small already (~50 KB)
    return Result("", blocks=[
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": f"Кадр камеры «{c['name']}» сейчас."}])


SKILL = Skill(
    name="eyes",
    summary="глаза: камеры дома - движение, лица, кто где был",
    tools=[Tool(
        name="eyes",
        description=("The home's cameras: now (motion, faces in frame, who is recognised; tracks - what is "
                     "watched; sees=false with why - a camera off, nothing watched) plus the journal of motion events with the people recognised "
                     "by face (similarity 0-1; 0.5+ is a match). Default: the last 2 hours; hours or since/until "
                     "(ISO) for another period, camera and person to narrow. A camera sees one room only: for "
                     "who is home, weigh it with the voices."),
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
