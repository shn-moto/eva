"""The ear's utterances straight from the hub (services/stt/hear.py), where the
brain runs too (docs/BRAIN-PLAN.md): no trip to the Station and back. Also
"someone is talking now" (hearing on/off), which the brain waits on before it
sends a request away, and the hearing states for the screen.
"""
import json
import queue
import threading
import time
import urllib.request

from core import body, profile
from core.util import log

URL = profile.service("stt").rsplit("/", 1)[0] + "/hear/events"
STATE = {"on": False, "at": 0.0, "active": False}
_phrases = queue.Queue()


def _worker(on_phrase):
    """The utterances one after another, in the order the ear made them: a thread
    each let the second part of a split phrase into the memory before the first
    (a book read aloud, 2026-11-25)."""
    while True:
        item = _phrases.get()
        try:
            on_phrase(item)
        except Exception as e:
            log(f"phrase failed: {e!r}")


def hearing():
    """A phrase is being heard right now (a stale "on" expires: a lost "off")."""
    return STATE["on"] and time.time() - STATE["at"] < 30


def listen(on_phrase):
    STATE["active"] = True
    while True:
        try:
            with urllib.request.urlopen(URL, timeout=40) as r:
                log("hub ear: connected")
                for line in r:
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    kind = ev.get("type")
                    if kind == "utterance":
                        _phrases.put(ev["item"])
                    elif kind == "hearing":
                        STATE.update(on=bool(ev.get("on")), at=time.time())
                    elif kind == "ui" and ev.get("state") != "stt":
                        e = {"type": "hear", "state": ev["state"]}
                        if ev.get("text"):
                            e["text"] = ev["text"]
                        body.ui(e)
        except Exception as e:
            log("hub ear:", e)
            time.sleep(2)


def start(on_phrase):
    threading.Thread(target=_worker, args=(on_phrase,), daemon=True).start()
    threading.Thread(target=listen, args=(on_phrase,), daemon=True).start()
