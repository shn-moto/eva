#!/usr/bin/env python3
"""Eva's screen server (127.0.0.1 only).

- serves the UI files of the current directory;
- GET /events: Server-Sent Events to the page (one queue per page);
- POST /api/event: any local process (eva_say.py, the assistant) sends a JSON
  event, e.g. {"type": "speak", ...} or {"type": "chat", "who": "eva", "text": ...};
  it goes to every connected page;
- POST /snap: the page sends one rendered frame (PNG data URL) into snaps/.

    python3 ui_server.py [port]
"""
import base64
import http.server
import json
import math
import os
import queue
import sys
import re
import threading
import time
import urllib.request
from collections import OrderedDict

PAGES = []                 # one queue per connected page
def _service_url(name, default):
    """As app/core/profile.service(name): env, else eva/profile.json, else the hub's default port."""
    if os.environ.get(f"EVA_{name.upper()}_URL"):
        return os.environ[f"EVA_{name.upper()}_URL"]
    try:
        sv = json.load(open(os.environ.get("EVA_PROFILE", "/opt/eva/eva/profile.json"), encoding="utf-8")).get("services", {})
    except (OSError, ValueError):
        sv = {}
    hub = os.environ.get("EVA_HUB", sv.get("hub", "127.0.0.1"))
    return sv.get(name, default).replace("{hub}", hub)


PHOTOLIB = _service_url("photolib", "http://{hub}:8000")
# pictures she drew and files sent to her live in the print service's inbox (services/print)
PRINT = _service_url("print", "http://{hub}:8909")
IMG_CACHE = os.environ.get("EVA_IMG_CACHE", "/var/cache/eva/img")
IMG_CACHE_MB = 60          # thumbnails and faces on disk (the root fs is small)
IMG_PATH = re.compile(r"^/(img)/(\d+)/(thumb|full)$|^/(face)/(\d+)$|^/(file)/([0-9a-f]{10})$")
FULL = OrderedDict()       # the last few full-size photos, in memory
FULL_LOCK = threading.Lock()


def photolib_image(kind, ident, size):
    """Images for the screen from photolib, through here: same origin for the
    page, cached so a reload or "назад" costs nothing. -> (bytes, type)"""
    url = (f"{PHOTOLIB}/image/{ident}/{size}" if kind == "img" else f"{PHOTOLIB}/faces/{ident}/thumb" if kind == "face"
           else f"{PRINT}/files/{ident}/jpeg?size=1280")
    key = f"{kind}-{ident}-{size}"
    if size == "full":
        with FULL_LOCK:
            if key in FULL:
                FULL.move_to_end(key)
                return FULL[key]
    else:
        path = os.path.join(IMG_CACHE, key + ".jpg")
        try:
            with open(path, "rb") as f:
                return f.read(), "image/jpeg"
        except OSError:
            pass
    with urllib.request.urlopen(url, timeout=15) as r:
        data, ctype = r.read(), r.headers.get("Content-Type", "image/jpeg")
    if size == "full":
        with FULL_LOCK:
            FULL[key] = (data, ctype)
            while len(FULL) > 6:
                FULL.popitem(last=False)
    else:
        os.makedirs(IMG_CACHE, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        prune_cache()
    return data, ctype


def prune_cache():
    files = [os.path.join(IMG_CACHE, n) for n in os.listdir(IMG_CACHE)]
    files = sorted(((os.path.getmtime(f), os.path.getsize(f), f) for f in files if os.path.isfile(f)))
    total = sum(sz for _, sz, _ in files)
    while files and total > IMG_CACHE_MB * 1024 * 1024:
        _, sz, f = files.pop(0)
        total -= sz
        os.remove(f)
PAGES_LOCK = threading.Lock()


class LedRing:
    """The 24-LED ring on top of the station shows what the screen shows: the
    attention left as an arc that shrinks, a cyan pulse while it hears a voice,
    a running light while she thinks, warm light while she talks (the
    original firmware's ring did this; so did the old assistant)."""

    N, FPS = 24, 20
    BASE = "/sys/class/leds/rgb:indicator-{}"

    def __init__(self):
        self.lock = threading.Lock()
        self.attn = {"until": 0, "total": 1, "speaking": False}
        self.hear, self.hear_until = "idle", 0.0
        self.think_until = 0.0
        self.vol, self.vol_until = 0.0, 0.0
        self.last = None
        try:
            for i in range(1, self.N + 1):
                with open(self.BASE.format(i) + "/brightness", "w") as f:
                    f.write("255")
            self.ok = True
        except OSError as e:
            print("LED ring not available:", e, file=sys.stderr)
            self.ok = False
        if self.ok:
            threading.Thread(target=self._loop, daemon=True).start()

    def event(self, ev):
        t, now = ev.get("type"), time.time()
        with self.lock:
            if t == "attention":
                self.attn = ev
            elif t == "volume":
                self.vol, self.vol_until = float(ev.get("level", 0)), now + 2.5
            elif t == "think":
                self.think_until = now + 25
            elif t in ("speak", "stop"):
                self.think_until = 0.0
                self.hear = "idle"
            elif t == "hear":
                st = ev.get("state")
                if st in ("on", "stt", "held", "called"):
                    self.hear, self.hear_until = st, now + (15 if st == "on" else 6)
                elif st in ("none", "text", "ignored"):
                    self.hear = "idle"
                    if st == "ignored":
                        self.think_until = 0.0

    def _frame(self, now):
        with self.lock:
            attn, hear, think = self.attn, self.hear if now < self.hear_until else "idle", now < self.think_until
            vol = self.vol if now < self.vol_until else None
        n = self.N
        if vol is not None:
            # volume as Alice shows it: the arc's length is the level, its ONE
            # colour too - quiet blue, then green, yellow, loud red
            lit = max(1, round(vol * n))
            stops = [(0.0, (0, 90, 255)), (0.35, (0, 220, 60)), (0.65, (255, 210, 0)), (1.0, (255, 0, 0))]
            for (a0, c0), (a1, c1) in zip(stops, stops[1:]):
                if vol <= a1:
                    k = (vol - a0) / (a1 - a0)
                    col = tuple(int(x + (y - x) * k) for x, y in zip(c0, c1))
                    break
            return [col if i < lit else (4, 4, 8) for i in range(n)]
        if think:                                        # running light: thinking
            head = int(now * 14) % n
            return [(v, v, v) for v in (200 if (head - i) % n == 0 else 90 if (head - i) % n == 1
                                        else 30 if (head - i) % n == 2 else 0 for i in range(n))]
        if attn.get("speaking"):                         # warm: talking
            return [(60, 45, 25)] * n
        if hear != "idle":                               # cyan pulse: hearing a voice
            v = int(70 + 60 * (0.5 + 0.5 * math.sin(now * 8)))
            return [(0, v, min(255, v + 40))] * n
        left = max(0.0, (attn.get("until", 0) / 1000 - now) / ((attn.get("total") or 1) / 1000))
        if left > 0:                                     # arc: attention left, shrinks to the top
            half = left * n / 2
            return [(0, 40, 60) if min(i, n - i) < half else (0, 0, 0) for i in range(n)]
        return [(0, 0, 0)] * n

    def _loop(self):
        while True:
            frame = self._frame(time.time())
            if frame != self.last:
                try:
                    for i, (r, g, b) in enumerate(frame):
                        if self.last is None or self.last[i] != (r, g, b):
                            with open(self.BASE.format(i + 1) + "/multi_intensity", "w") as f:
                                f.write(f"{r} {g} {b}")
                    self.last = frame
                except OSError:
                    pass
            time.sleep(1 / self.FPS)


RING = None


AVATAR_FILE = "/var/lib/eva/avatar"     # ascii | image | eq: kept over reloads and restarts


def avatar():
    try:
        return open(AVATAR_FILE).read().strip() or "ascii"
    except OSError:
        return "ascii"


LAST_VIEW = {"ev": None}     # what is on the screen: a reloaded page gets it (it fell back to the face
                             # while music played after a restart of the page, 2026-11-20)


PANELS_FILE = "/var/lib/eva/panels.json"    # which panels are on: chat (right), stats (top)
LAST_STATS = {"ev": None}                    # the status bar's numbers for a page that connects


def panels():
    try:
        return dict({"chat": True, "stats": False}, **json.load(open(PANELS_FILE)))
    except (OSError, ValueError):
        return {"chat": True, "stats": False}


def broadcast(event):
    if event.get("type") == "stats":
        LAST_STATS["ev"] = event
    if event.get("type") == "panel" and event.get("panel") in ("chat", "stats"):
        p = panels()
        p[event["panel"]] = bool(event.get("on"))
        try:
            with open(PANELS_FILE, "w") as f:
                json.dump(p, f)
        except OSError:
            pass
    if event.get("type") == "view":
        LAST_VIEW["ev"] = None if event.get("kind") == "close" else event
    if event.get("type") == "avatar":
        try:
            with open(AVATAR_FILE, "w") as f:
                f.write(str(event.get("mode", "ascii")))
        except OSError:
            pass
    if RING:
        RING.event(event)
    data = json.dumps(event, ensure_ascii=False)
    with PAGES_LOCK:
        for q in list(PAGES):
            try:
                q.put_nowait(data)
            except queue.Full:   # a stalled page must not hold the others up
                PAGES.remove(q)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if not self.path.startswith(("/events", "/api/")):
            return
        super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path == "/api/status":
            with PAGES_LOCK:
                body = json.dumps({"pages": len(PAGES)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = IMG_PATH.match(self.path)
        if m:
            kind, ident, size = ((m.group(1), m.group(2), m.group(3)) if m.group(1) else
                                 (m.group(4), m.group(5), "thumb") if m.group(4) else (m.group(6), m.group(7), "full"))
            try:
                data, ctype = photolib_image(kind, ident, size)
            except OSError as e:
                self.send_error(502, f"{kind}: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path != "/events":
            return super().do_GET()
        q = queue.Queue(maxsize=64)
        with PAGES_LOCK:
            PAGES.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": hello\n\n")
            # the page starts with the avatar chosen last
            self.wfile.write(f"data: {json.dumps({'type': 'avatar', 'mode': avatar()})}\n\n".encode())
            for name, on in panels().items():
                self.wfile.write(f"data: {json.dumps({'type': 'panel', 'panel': name, 'on': on})}\n\n".encode())
            if LAST_STATS["ev"]:
                self.wfile.write(f"data: {json.dumps(LAST_STATS['ev'])}\n\n".encode())
            if LAST_VIEW["ev"]:
                self.wfile.write(f"data: {json.dumps(LAST_VIEW['ev'], ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")   # keep-alive, notices a closed page
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with PAGES_LOCK:
                if q in PAGES:
                    PAGES.remove(q)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/event":
            try:
                event = json.loads(body)
            except ValueError:
                self.send_error(400, "not JSON")
                return
            broadcast(event)
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/snap":
            head, _, data = body.partition(b",")
            if not head.startswith(b"data:image/png"):
                self.send_error(400)
                return
            os.makedirs("snaps", exist_ok=True)
            tag = "".join(c for c in self.headers.get("X-Name", "") if c.isalnum() or c in "-_") or "snap"
            name = os.path.join("snaps", f"{tag}-{time.strftime('%H%M%S')}.png")
            with open(name, "wb") as f:
                f.write(base64.b64decode(data))
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    if os.path.exists(LedRing.BASE.format(1)):
        RING = LedRing()
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
