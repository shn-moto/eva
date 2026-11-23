#!/usr/bin/env python3
"""Eva's brain: wiring of the core, the skills and the channels
(docs/ARCHITECTURE.md). Abilities live in app/skills, the voice conversation in
dialog.py, text channels in chat.py.

    POST 127.0.0.1:8081/utterance   {"text", "voice", "speech_start", ...}   (ear.py)
    POST 127.0.0.1:8081/chat        {"text", "telegram_id" | "who", "key"} -> {"reply", "show"}  (telegram.py)
"""
import http.server
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic  # noqa: E402

import chat  # noqa: E402
import dialog  # noqa: E402
import memory  # noqa: E402
import music  # noqa: E402
import photolib  # noqa: E402
import say  # noqa: E402
import slideshow  # noqa: E402
from core import cost, identity, llm, screen, skills, speech  # noqa: E402
from core.speech import ui  # noqa: E402
from core.util import log, secrets  # noqa: E402


def chat_person(req):
    """The text channel's sender as a Person: a photolib user by Telegram id."""
    tid = req.get("telegram_id")
    if tid is not None:
        u = photolib.user_by_telegram(tid)
        if u:
            return identity.from_telegram(u)
    return identity.GUEST


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        out = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/chat":
            try:
                req = json.loads(body)
                person = chat_person(req)
                if not person.known:
                    return self._json(403, {"error": "unknown sender"})
                text, shows = chat.reply(req["text"], person, req.get("key") or f"chat:{person.person_id}")
                return self._json(200, {"reply": text, "show": shows})
            except (ValueError, KeyError):
                return self._json(400, {"error": "bad request"})
            except anthropic.APIError as e:
                log("chat error:", e)
                return self._json(200, {"reply": "Ой, я сейчас не могу ответить, попробуй чуть позже.", "show": []})
        self.send_response(204)
        self.end_headers()
        if self.path == "/utterance":
            try:
                dialog.on_phrase(json.loads(body))
            except ValueError:
                pass


def start_slideshow():
    """Family photos on the TV when nobody needs her (app/slideshow.py)."""
    import time
    started = time.time()                # a fresh start counts as activity: no slideshow right away

    def busy():
        return bool(dialog.music_down()) or dialog.PENDING["text"] is not None

    def last_activity():
        # music is not activity: the slideshow runs over it and names the track
        return max(dialog.state["last_spoke"], dialog.state.get("asked_at", 0.0), screen.state["touched"],
                   dialog.VOICE.busy_until, started)

    def screen_free():
        return screen.state["p"] is None

    threading.Thread(target=slideshow.run, args=(busy, last_activity, screen_free, screen.back),
                     daemon=True).start()


if __name__ == "__main__":
    skills.load()
    skills.start_refresher()
    log(f"history restored: {dialog.load_history()} messages")
    threading.Thread(target=dialog.worker, daemon=True).start()
    threading.Thread(target=dialog.attention_watch, daemon=True).start()
    threading.Thread(target=screen.watch, daemon=True).start()
    # a restart ends the music (mpg123 was ours): the TV must not keep showing the player
    music._flag(False)
    speech.ui({"type": "view", "kind": "close"})
    # the music goes down when she is called by name, answers or talks
    music.PLAYER.busy = dialog.music_down
    # the status bar on the TV: the numbers after every Claude call, and now
    cost.on_change = lambda: threading.Thread(target=ui, args=(cost.stats(),), daemon=True).start()
    cost.on_change()
    threading.Thread(target=music.PLAYER.duck_watch, daemon=True).start()
    start_slideshow()
    # "секунду, посмотрю" must sound at once: its speech is made now, once, and cached
    threading.Thread(target=lambda: [say.tts(f) for f in dialog.FILLERS], daemon=True).start()
    threading.Thread(target=speech.buttons, args=(dialog.VOICE,), daemon=True).start()
    threading.Thread(target=speech.volume_ring, daemon=True).start()
    dsn = secrets().get("ASSISTANT_PG_DSN")
    if dsn:
        threading.Thread(target=memory.shipper, args=(dsn, log), daemon=True).start()
    log(f"voices known: {dialog.VOICES.names()}, guests: {len(dialog.VOICES.clusters)}")
    log(f"brain up: {llm.MODEL}, effort {llm.EFFORT}")
    http.server.ThreadingHTTPServer(("127.0.0.1", 8081), Handler).serve_forever()
