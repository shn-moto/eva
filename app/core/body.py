"""The brain's line to Eva's body on the Station (app/body.py): commands out,
events in. The brain may run anywhere (docs/BRAIN-PLAN.md); the speaker, the
screen, the ring and the keys are there.
"""
import base64
import json
import queue
import os
import threading
import time
import urllib.parse
import urllib.request

from core.util import log

URL = os.environ.get("EVA_BODY_URL", "http://127.0.0.1:8085")
_handlers = {}                  # event type -> [fn(event)]


def on(kind, fn):
    _handlers.setdefault(kind, []).append(fn)


def post(path, obj=None, data=None, timeout=5):
    """A command; -> the answer (dict) or None when the body is away."""
    if data is None:
        data, ctype = json.dumps(obj or {}, ensure_ascii=False).encode(), "application/json"
    else:
        ctype = "application/octet-stream"
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + path, data=data, headers={"Content-Type": ctype}),
                                    timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (OSError, ValueError) as e:
        log(f"body {path.split('?')[0]}: {e}")
        return None


def get(path, timeout=3):
    try:
        with urllib.request.urlopen(URL + path, timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


_ui_q = queue.Queue()


def _ui_sender():
    while True:
        post("/ui", _ui_q.get(), timeout=3)


def ui(event):
    """An event for the screen - not waited for, but in order: one sender, one
    queue (a "close" overtaking the "open" before it left the TV on the wrong view)."""
    _ui_q.put(event)


threading.Thread(target=_ui_sender, daemon=True).start()


def speech(gen, sid, text, wav_path, frames):
    """A sentence to play: the sound and its lips (visemes), both made here."""
    with open(wav_path, "rb") as f:
        data = f.read()
    return post("/speech", {"gen": gen, "id": sid, "text": text, "frames": frames,
                            "wav": base64.b64encode(data).decode()}, timeout=10)


BOOT = {"id": None}


def _check_boot():
    """A new boot of the Station (its boot id changed, up < 5 min): the "boot" handlers run once."""
    h = get("/health") or {}
    bid = h.get("boot_id")
    if not bid or bid == BOOT["id"]:
        return
    first = BOOT["id"] is None
    BOOT["id"] = bid
    if first and (h.get("uptime") or 0) > 60:
        return                            # the brain (re)started, the Station has been up a while: no greeting
    if (h.get("uptime") or 0) > 300:
        return
    log(f"body: the Station booted {h.get('uptime')} s ago")
    for fn in _handlers.get("boot", []):
        threading.Thread(target=fn, args=(h,), daemon=True).start()


def listen():
    """The body's events -> the handlers. Runs forever, reconnects."""
    while True:
        try:
            with urllib.request.urlopen(URL + "/events", timeout=40) as r:
                log("body events: connected")
                _check_boot()
                for line in r:
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    for fn in _handlers.get(ev.get("type"), []):
                        try:
                            fn(ev)
                        except Exception as e:      # one handler's error must not stop the stream
                            log(f"body event {ev.get('type')}: {e}")
        except Exception as e:
            log("body events:", e)
            time.sleep(2)


def start():
    threading.Thread(target=listen, daemon=True).start()
