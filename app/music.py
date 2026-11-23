"""The music player on the Station: YouTube Music streamed as MP3 by the music
service on shn-linux (services/music), played by one mpg123 in
remote-control mode (-R) on the "eva" ALSA mixer (/etc/asound.conf), so her
voice can sound over it. While she speaks or listens the music is turned down.

Ported from the old assistant.py (yt-dlp -> ffmpeg -> mpg123 on the Station);
the Station has no room for yt-dlp and ffmpeg now, so they run on shn-linux.

The TV shows the player (kind "music"); other views (photos) go over it and
the player comes back when they close (core/screen.py).
"""
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from core.speech import ui, volume_get, volume_set
from core import profile
from core.util import log

SERVICE = profile.service("music")
DEVICE = "eva"
DUCK = profile.tune("music.duck", 0.2)                     # the music's share of its volume while she talks or listens
VOLUME_STEP = profile.tune("music.volume_step", 5)                # % of the speakers' master per «громче» / «тише»


MUSIC_FLAG = "/run/eva/music"     # ear.py: music plays -> listen in short windows over it


def _flag(on):
    try:
        if on:
            open(MUSIC_FLAG, "w").close()
        elif os.path.exists(MUSIC_FLAG):
            os.remove(MUSIC_FLAG)
    except OSError:
        pass


def _get(path, timeout=150):
    with urllib.request.urlopen(SERVICE + path, timeout=timeout) as r:
        return json.loads(r.read())


def search(query, kind="track", limit=1):
    return _get(f"/search?q={urllib.parse.quote(query)}&kind={kind}&limit={limit}")


class Player:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.queue, self.index = [], 0
        self.playing = False           # a track is loaded and not paused
        self.paused = False
        self.volume = 70               # mpg123's own level: the music's share against her voice, fixed
        self.ducked = False
        self.loaded_at = 0.0
        self.loading = False
        self.started = 0.0             # when the current track began (minus paused time)
        self.paused_at = 0.0
        self.busy = lambda: False      # set by the brain: she talks or listens -> duck

    # --- mpg123 ---
    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(["mpg123", "-R", "-o", "alsa", "-a", DEVICE],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._cmd("SILENCE")           # no per-frame progress lines
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        self._apply_volume()

    def _cmd(self, c):
        try:
            self.proc.stdin.write(c + "\n")
            self.proc.stdin.flush()
        except (OSError, AttributeError) as e:
            log(f"music: mpg123 command {c!r}: {e}")

    def _read(self, proc):
        for line in proc.stdout:
            line = line.strip()
            # "@P 0": the track ended. Right after a LOAD it is the old track's
            # end (whether mpg123 says it then is not certain) - not ours.
            if line == "@P 0" and time.time() - self.loaded_at > 2:
                threading.Thread(target=self._ended, daemon=True).start()
            elif line.startswith("@S") and self.loading:
                # mpg123 has begun to decode: the sound starts now - not at LOAD,
                # 3-5 s earlier, while shn-linux finds the stream (the progress ran ahead)
                self.loading = False
                self.started = time.time()
                self.view()
            elif line.startswith("@E"):
                log(f"music: mpg123 {line}")

    def _apply_volume(self):
        v = self.volume * (DUCK if self.ducked else 1.0)
        self._cmd(f"VOLUME {int(v)}")

    # --- the queue ---
    def play(self, tracks):
        with self.lock:
            self._ensure()
            self.queue, self.index = list(tracks), 0
            self._load()

    def _load(self):
        t = self.queue[self.index]
        self.loaded_at = time.time()
        self._cmd("LOAD " + SERVICE + "/stream?url=" + urllib.parse.quote(t["url"], safe=""))
        self.playing, self.paused, self.started = True, False, time.time()
        self.loading = True                   # until mpg123 says the sound began ("@S")
        _flag(True)
        log(f"music: playing {t.get('title')!r} ({self.index + 1}/{len(self.queue)})")
        self.view()
        if not t.get("info"):
            threading.Thread(target=self._info, args=(t,), daemon=True).start()
        if "bpm" not in t:
            threading.Thread(target=self._beats, args=(t,), daemon=True).start()

    def _beats(self, t):
        """Tempo and first beat (music_server /beats): she dances to them on the TV."""
        try:
            b = _get("/beats?url=" + urllib.parse.quote(t["url"], safe=""), 150)
            t.update(bpm=b.get("bpm") or 0, first=b.get("first") or 0)
            log(f"music: {t.get('title')!r} {t['bpm']} BPM (strength {b.get('strength')})")
        except (OSError, ValueError) as e:
            t.update(bpm=0, first=0)
            log(f"music: beats: {e}")
            return
        if self.now() is t:
            self.view()

    def _info(self, t):
        """Artist, album, length, cover: the search does not give them."""
        try:
            t.update({k: v for k, v in _get("/info?url=" + urllib.parse.quote(t["url"], safe=""), 60).items() if v})
            t["info"] = True
        except (OSError, ValueError) as e:
            log(f"music: info {t.get('title')!r}: {e}")
        if self.queue and self.queue[self.index] is t:
            self.view()

    def _ended(self):
        with self.lock:
            if not self.playing:
                return
            if self.index + 1 < len(self.queue):
                self.index += 1
                self._load()
            else:
                log("music: queue finished")
                self._clear()

    def _clear(self):
        self.playing, self.paused, self.queue, self.index = False, False, [], 0
        _flag(False)
        from core import screen
        screen.music_closed()

    # --- control ---
    def active(self):
        return self.playing

    def pause(self, on=True):
        with self.lock:
            if not self.playing or self.paused == on:
                return
            self._cmd("PAUSE")         # a toggle in mpg123
            self.paused = on
            _flag(not on)
            if on:
                self.paused_at = time.time()
            else:
                self.started += time.time() - self.paused_at
            self.view()

    def next(self, step=1):
        with self.lock:
            if not self.playing:
                return False
            i = self.index + step
            if not 0 <= i < len(self.queue):
                return False
            if self.paused:
                self._cmd("PAUSE")
            self.index = i
            self._load()
            return True

    def stop(self):
        with self.lock:
            if not self.playing:
                return
            self._cmd("STOP")
            log("music: stopped")
            self._clear()

    def louder(self, up=True):
        """The speakers' master, as the ring turns it: the player showed its own
        70 % while the ring was elsewhere (2026-11-21)."""
        volume_set(max(5, volume_get() + (VOLUME_STEP if up else -VOLUME_STEP)))

    def duck_watch(self):
        """Down while she is wanted (dialog.music_down), back after."""
        while True:
            time.sleep(0.15)
            if not self.playing:
                continue
            want = bool(self.busy())
            if want != self.ducked:
                with self.lock:
                    self.ducked = want
                    self._apply_volume()

    # --- for the screen and the model ---
    def now(self):
        return self.queue[self.index] if self.playing and self.queue else None

    def position(self):
        if not self.playing:
            return 0.0
        return (self.paused_at if self.paused else time.time()) - self.started

    def describe(self):
        t = self.now()
        if not t:
            return "Музыка не играет."
        nxt = ", ".join(x.get("title") or "?" for x in self.queue[self.index + 1:self.index + 4])
        return (f"{'На паузе' if self.paused else 'Играет'}: {t.get('title')}"
                + (f" - {t['artist']}" if t.get("artist") else "")
                + f", трек {self.index + 1} из {len(self.queue)}, громкость {volume_get()}%"
                + (f". Дальше: {nxt}" if nxt else ""))

    def view_event(self):
        t = self.now()
        if not t:
            return None
        return {"type": "view", "kind": "music",
                "now": {k: t.get(k) for k in ("title", "artist", "album", "year", "duration", "thumb")},
                "pos": 0 if self.loading else round(self.position(), 1), "at": int(time.time() * 1000),
                "paused": self.paused, "loading": self.loading,
                "volume": volume_get(), "bpm": t.get("bpm") or 0, "first": t.get("first") or 0,
                "index": self.index + 1, "count": len(self.queue),
                "next": [{"title": x.get("title"), "artist": x.get("artist"), "thumb": x.get("thumb")}
                         for x in self.queue[self.index + 1:self.index + 6]]}

    def view(self):
        """The player on the TV - unless a picture view is open over it."""
        from core import screen
        if screen.state["p"] is None:
            ev = self.view_event()
            if ev:
                ui(ev)


PLAYER = Player()
