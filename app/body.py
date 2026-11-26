#!/usr/bin/env python3
"""Eva's body on the Station: what has to happen where the speaker, the screen,
the ring and the keys are (docs/BRAIN-PLAN.md, stage 1). The brain - wherever
it runs - sends commands here and listens to the events.

    POST /speech {"gen", "id", "text", "frames", "wav"}   her sentence (base64 wav + its lips): played in order
    POST /speech/stop {"gen"}      cut her now; queued sentences of older replies go
    POST /music {"cmd"}            a command to mpg123 (remote mode); its lines come as events
    POST /flag {"name": "music", "on"}   /run/eva/music for the ear (listen in windows)
    POST /volume {"pct", "show"}   the speakers' master;  GET /volume
    POST /ui {...}                 an event for the screen (ui_server)
    GET  /events                   server-sent events: speech start/end, key, mpg123, volume
    GET  /health

Timestamps in the events are this device's clock - the ear's clock, so "was
she speaking when this phrase began" compares like with like.
"""
import base64
import http.server
import ipaddress
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import say
from core import profile
from core.util import log

PORT = int(os.environ.get("EVA_BODY_PORT", "8085"))
UI = "http://127.0.0.1:8080/api/event"
SPEAKING_FLAG = "/run/eva/speaking"   # exists while her voice plays (ear.py reads it)
FLAGS = {"music": "/run/eva/music"}   # ear.py: music plays -> listen in short windows over it
VOLUME_FILE = "/var/lib/eva/volume"   # percent, kept over restarts
VOL_DB = profile.tune("speech.volume_db", (-60.0, -15.0))      # 0 % .. 100 % of the speakers' master (0.125 dB a step, 1023 = +24 dB)
VOL_STEP = profile.tune("speech.volume_step", 0.5)               # % per click of the ring (4 was far too sensitive, 1 still a bit)
WAV_DIR = os.path.join(tempfile.gettempdir(), "eva-speech")


# ---- events to the brain ----
class Events:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, ev):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


EVENTS = Events()


def ui(event):
    """To the screen, fire and forget: nothing here waits for the page."""
    def post():
        try:
            urllib.request.urlopen(urllib.request.Request(
                UI, data=json.dumps(event, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"}), timeout=2).read()
        except OSError:
            pass
    threading.Thread(target=post, daemon=True).start()


def flag(path, on):
    try:
        if on:
            # /run is empty after a reboot: the flag's folder is made here (os/rootfs tmpfiles
            # eva.conf too) - without it the ear never knew music played (2026-11-26)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---- her voice ----
class Speaker:
    def __init__(self):
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.min_gen = 0                  # sentences of older replies are dropped (stop)
        self.play = None
        self.current = None
        os.makedirs(WAV_DIR, exist_ok=True)
        flag(SPEAKING_FLAG, False)
        threading.Thread(target=self._player, daemon=True).start()

    def put(self, gen, sid, text, wav_bytes, frames):
        """frames: the lips (visemes per 20 ms), made by the brain from this sound."""
        if gen < self.min_gen:
            return False
        path = os.path.join(WAV_DIR, f"{sid}.wav")
        with open(path, "wb") as f:
            f.write(wav_bytes)
        self.q.put((gen, sid, text, path, frames))
        return True

    def busy(self):
        return self.play is not None or not self.q.empty()

    def stop(self, gen=None):
        """Cut now. gen: the brain's new reply number - anything older goes."""
        with self.lock:
            self.min_gen = max(self.min_gen, gen if gen is not None else self.min_gen + 1)
            while not self.q.empty():
                try:
                    _, _, _, p, _ = self.q.get_nowait()
                    os.remove(p)
                except (queue.Empty, OSError):
                    pass
            if self.play:
                self.play.kill()
        flag(SPEAKING_FLAG, False)

    def _player(self):
        while True:
            gen, sid, text, path, track = self.q.get()
            try:
                if gen < self.min_gen:
                    continue
                start = time.time() + 0.06
                ui({"type": "speak", "start": start * 1000 + say.latency_ms(), "hop": say.HOP * 1000,
                    "frames": track, "text": text})
                while time.time() < start:
                    time.sleep(0.002)
                with self.lock:
                    if gen < self.min_gen:
                        continue
                    flag(SPEAKING_FLAG, True)
                    self.current = sid
                    self.play = subprocess.Popen(["aplay", "-q", "-D", say.PLAY_DEV,
                                                  f"--buffer-time={say.BUFFER_US}", path])
                    t0 = time.time()
                EVENTS.publish({"type": "speech", "state": "start", "id": sid, "gen": gen, "text": text, "t": t0})
                self.play.wait()
                t1 = time.time()
                self.play, self.current = None, None
                if self.q.empty():
                    flag(SPEAKING_FLAG, False)
                EVENTS.publish({"type": "speech", "state": "end", "id": sid, "gen": gen, "t": t1})
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass


SPEAKER = None


# ---- music: one mpg123 in remote mode ----
class Mpg123:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def cmd(self, c):
        with self.lock:
            if not (self.proc and self.proc.poll() is None):
                self.proc = subprocess.Popen(["mpg123", "-R", "-o", "alsa", "-a", say.PLAY_DEV],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
                self.proc.stdin.write("SILENCE\n")         # no per-frame progress lines
                threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
            try:
                self.proc.stdin.write(c + "\n")
                self.proc.stdin.flush()
            except OSError as e:
                log(f"mpg123 command {c!r}: {e}")

    def _read(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("@") and line != "@P 2":
                EVENTS.publish({"type": "mpg123", "line": line, "t": time.time()})


MUSIC = Mpg123()


# ---- the speakers' master ----
VOL = {"pct": None, "want": None, "show": False, "mixer": None}
VOL_LOCK = threading.Condition()


def volume_get():
    if VOL["want"] is not None:
        return VOL["want"]
    try:
        return max(0.0, min(100.0, float(open(VOLUME_FILE).read().strip())))
    except (OSError, ValueError):
        return 55.0                       # -35 dB: the level station-audio-init.sh sets


def _mixer_write(pct):
    """Both amplifiers through one amixer kept open (-s: commands on stdin) -
    a fork per click was most of the ring's lag."""
    db = VOL_DB[0] + (VOL_DB[1] - VOL_DB[0]) * pct / 100
    raw = 0 if pct <= 0 else int(round(1023 - (24.0 - db) / 0.125))
    cmd = "".join(f'cset "name={p} Master Playback Volume" {raw}\n' for p in ("Subwoofer", "Tweeters"))
    for _ in range(2):
        m = VOL["mixer"]
        if m is None or m.poll() is not None:
            m = VOL["mixer"] = subprocess.Popen(["amixer", "-q", "-c", "snddaudio1", "-s"],
                                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, text=True)
        try:
            m.stdin.write(cmd)
            m.stdin.flush()
            return
        except OSError:
            VOL["mixer"] = None


def _volume_apply():
    """Applies the latest level asked for: clicks faster than the mixer and the
    screen are merged, not queued (the ring lagged by seconds, 2026-11-21)."""
    while True:
        with VOL_LOCK:
            while VOL["want"] is None or VOL["want"] == VOL["pct"]:
                VOL_LOCK.wait()
            pct, show = VOL["want"], VOL["show"]
            VOL["pct"], VOL["show"] = pct, False
        _mixer_write(pct)
        if show:
            ui({"type": "volume", "level": round(pct / 100, 4)})    # the LED ring: an arc from blue to red
        EVENTS.publish({"type": "volume", "pct": pct})
        try:
            with open(VOLUME_FILE, "w") as f:
                f.write(f"{pct:g}")
        except OSError:
            pass


def volume_set(pct, show=True):
    pct = max(0.0, min(100.0, round(float(pct) * 2) / 2))
    with VOL_LOCK:
        VOL["want"] = pct
        VOL["show"] = VOL["show"] or show
        if VOL["pct"] == pct and show:
            VOL["pct"] = None                # same level: show it anyway
        VOL_LOCK.notify()
    return pct


def _input(name):
    for e in sorted(os.listdir("/sys/class/input")):
        try:
            if open(f"/sys/class/input/{e}/device/name").read().strip() == name:
                return "/dev/input/" + e
        except OSError:
            pass
    return None


def volume_ring():
    """The rotary ring on top of the Station (relative steps)."""
    dev = _input("rotary-encoder")
    if not dev:
        log("no rotary-encoder device")
        return
    pct = volume_set(volume_get(), show=False)
    with open(dev, "rb") as f:
        while True:
            ev = f.read(24)
            etype, value = int.from_bytes(ev[16:18], "little"), int.from_bytes(ev[20:24], "little", signed=True)
            if etype == 2 and value:                       # EV_REL: one click
                pct = volume_set(volume_get() + VOL_STEP * value)


def buttons():
    """Any Station key stops her at once, here; the brain hears of it too."""
    dev = _input("gpio-keys")
    if not dev:
        log("no gpio-keys device")
        return
    with open(dev, "rb") as f:
        while True:
            ev = f.read(24)
            etype, code, value = (int.from_bytes(ev[16:18], "little"), int.from_bytes(ev[18:20], "little"),
                                  int.from_bytes(ev[20:24], "little", signed=True))
            if etype == 1 and value == 1:
                if SPEAKER.busy():
                    log(f"key {code}: stop")
                    SPEAKER.stop()
                    ui({"type": "stop"})
                EVENTS.publish({"type": "key", "code": code, "t": time.time()})


# ---- HTTP ----
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _home(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            return ip.is_private or ip.is_loopback
        except ValueError:
            return False

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._home():
            return self._json(403, {"error": "home network only"})
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True, "speaking": SPEAKER.busy()})
        if path == "/volume":
            return self._json(200, {"pct": volume_get()})
        if path == "/events":
            q = EVENTS.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(b": hello\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                EVENTS.unsubscribe(q)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._home():
            return self._json(403, {"error": "home network only"})
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            b = json.loads(body or b"{}")
            if u.path == "/speech":
                ok = SPEAKER.put(int(b["gen"]), str(b["id"]), b.get("text", ""), base64.b64decode(b["wav"]),
                                 b.get("frames") or [])
                return self._json(200, {"queued": ok})
            if u.path == "/speech/stop":
                SPEAKER.stop(b.get("gen"))
                return self._json(200, {"ok": True})
            if u.path == "/music":
                MUSIC.cmd(str(b["cmd"]))
                return self._json(200, {"ok": True})
            if u.path == "/flag":
                flag(FLAGS[b["name"]], bool(b.get("on")))
                return self._json(200, {"ok": True})
            if u.path == "/volume":
                return self._json(200, {"pct": volume_set(b["pct"], bool(b.get("show", True)))})
            if u.path == "/ui":
                ui(b)
                return self._json(200, {"ok": True})
        except (KeyError, ValueError) as e:
            return self._json(400, {"error": str(e)})
        self._json(404, {"error": "not found"})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main():
    global SPEAKER
    SPEAKER = Speaker()
    flag(FLAGS["music"], False)
    threading.Thread(target=_volume_apply, daemon=True).start()
    threading.Thread(target=volume_ring, daemon=True).start()
    threading.Thread(target=buttons, daemon=True).start()
    log(f"body up on :{PORT}")
    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
