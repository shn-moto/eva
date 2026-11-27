"""The music player: YouTube Music streamed as MP3 by the music service on
shn-linux (services/music), played by one mpg123 in remote-control mode (-R)
on the Station's "eva" ALSA mixer, so her voice can sound over it - mpg123
runs in the body (app/body.py), driven from here by commands and its lines. While she speaks or listens the music is turned down.

Ported from the old assistant.py (yt-dlp -> ffmpeg -> mpg123 on the Station);
the Station has no room for yt-dlp and ffmpeg now, so they run on shn-linux.

The TV shows the player (kind "music"); other views (photos) go over it and
the player comes back when they close (core/screen.py).
"""
import json
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request

from core.speech import ui, volume_get, volume_set
from core import body, profile
from core.util import log

SERVICE = profile.service("music")
DUCK = profile.tune("music.duck", 0.2)                     # the music's share of its volume while she talks or listens
VOLUME_STEP = profile.tune("music.volume_step", 10)               # % of the master per «громче» / «тише» (5 % = 2 dB was not heard, 2026-11-23)


EARLY_END = 30                    # s: a longer track "ending" before this broke off - played once more


def clean(text):
    """A title for the TV and her voice: no emoji. The Station has DejaVu only;
    emoji (🎧🔥 in YouTube titles) came out as boxes - "broken encoding" on the
    screen (2026-11-23). Symbols DejaVu has (♫ ☺) stay."""
    if not text:
        return text
    out = "".join(ch for ch in text if ord(ch) <= 0xFFFF and not 0xFE00 <= ord(ch) <= 0xFE0F and ch != "‍")
    return " ".join(out.split())


def _flag(on):
    """The ear's flag on the Station: music plays -> listen in short windows over it."""
    threading.Thread(target=body.post, args=("/flag", {"name": "music", "on": bool(on)}), daemon=True).start()


AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")


def _get(path, timeout=150):
    with urllib.request.urlopen(SERVICE + path, timeout=timeout) as r:
        return json.loads(r.read())


def search(query, kind="track", limit=1):
    """One track comes with the next search hits as `alts`: YouTube Music's first hit can be a
    video that is "not available" here, and a listener's song never played (2026-11-27 12:23)."""
    u = query.strip()
    if u.startswith("/") and not u.lower().endswith(AUDIO_EXT):
        # a folder on the hub (/media or a music root of «Ресурсы»): all its tracks, shuffled
        # (a listener «сборка», 2026-11-27)
        files = _get(f"/library/files?kind=music&dir={urllib.parse.quote(u)}", timeout=20)
        tracks = [{"title": os.path.splitext(f.rsplit("/", 1)[-1])[0], "url": f} for f in files]
        random.shuffle(tracks)
        return tracks
    if re.match(r"https?://\S+$", u) or u.startswith("/"):
        # a link, not words: a listener's exact track chosen on the admin page (the search's
        # order drifts - the same words gave the Leopold song, then «Живите дружно», 2026-11-27)
        # or a direct mp3, or a local file "/media/<name>" on the hub (music_server.source)
        u = query.strip()
        return [{"title": urllib.parse.unquote(u.rsplit("/", 1)[-1].split("?")[0]) or u, "url": u}]
    spare = 3 if kind == "track" and limit == 1 else 0
    found = _get(f"/search?q={urllib.parse.quote(query)}&kind={kind}&limit={limit + spare}")
    if spare and found:
        found[0]["alts"] = found[1:]
        found = found[:1]
    return found


def listener_log(t, ok, what):
    """A listener's music request ends here: its status line for the admin page."""
    lid = t.pop("listener", None)
    if lid:
        from core.listeners import tag
        log(f"{tag(lid)}: {'ok' if ok else 'error'}: {what}")


class Player:
    def __init__(self):
        self.lock = threading.Lock()
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
        body.on("mpg123", self._line)

    # --- mpg123, in the body (app/body.py) ---
    def _ensure(self):
        """The body starts mpg123 at the first command; the level is set again."""
        self._apply_volume()

    def _cmd(self, c):
        body.post("/music", {"cmd": c})

    def _line(self, ev):
        line = ev.get("line", "")
        # "@P 0": the track ended. Right after a LOAD it is the old track's
        # end (whether mpg123 says it then is not certain) - not ours.
        if line == "@P 0" and time.time() - self.loaded_at > 2:
            threading.Thread(target=self._ended, daemon=True).start()
        elif line.startswith("@S") and self.loading:
            # mpg123 has begun to decode: the sound starts now - not at LOAD,
            # 3-5 s earlier, while shn-linux finds the stream (the progress ran ahead)
            self.loading = False
            self.started = time.time()
            if self.queue:
                t = self.queue[self.index]
                listener_log(t, True, f"playing {t.get('title')!r}")
            self.view()
        elif line.startswith(("@E", "@I", "@P")) and line != "@P 2":
            log(f"music: mpg123 {line[:160]}")    # errors, stream info, state: why a track stopped

    def _apply_volume(self):
        v = self.volume * (DUCK if self.ducked else 1.0)
        self._cmd(f"VOLUME {int(v)}")

    # --- the queue ---
    def play(self, tracks):
        # the media library's tiles give way to the player, whoever started the music: a listener's
        # «включи сборку» played and the tiles stayed on the TV (2026-11-27 21:00)
        try:
            from core import screen
            if (screen.state.get("p") or {}).get("kind") == "library":
                screen.act("close")
        except Exception as e:
            log(f"music: tiles not closed: {e}")
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
            if (t.get("thumb") or "").startswith("/"):
                t["thumb"] = SERVICE + t["thumb"]         # a local file's cover.jpg, served by the music service
            t["info"] = True
            if t.get("error"):
                log(f"music: info {t.get('title')!r}: {t['error']}")
        except (OSError, ValueError) as e:
            log(f"music: info {t.get('title')!r}: {e}")
        if self.queue and self.queue[self.index] is t:
            self.view()

    def _ended(self):
        with self.lock:
            if not self.playing:
                return
            # a track that "ended" in its first seconds broke off (a track stopped
            # after 10 s and the screen went back to the face, 2026-11-23): once more
            t = self.queue[self.index] if self.queue else {}
            played = time.time() - self.started
            # an unknown length counts as long: /info fails with the same hiccup that broke the
            # stream, and a listener's song never played at all (2026-11-27 10:42)
            retry = played < EARLY_END and (t.get("duration") or 999) > 60 and not t.get("retried")
            if retry:
                t["retried"] = True
                log(f"music: {t.get('title')!r} ended after {played:.0f}s of {t.get('duration') or '?'}s - again")
        if retry:
            time.sleep(2)                     # a hiccup of YouTube / yt-dlp passes in a moment (lock free: «стоп» works)
            with self.lock:
                if self.playing and self.queue and self.queue[self.index] is t:
                    self._load()
            return
        with self.lock:
            if not self.playing:
                return
            if played < EARLY_END and t.get("alts"):
                # it would not play even twice: the next search hit instead of silence
                nxt = dict(t["alts"][0], alts=t["alts"][1:])
                if t.get("listener"):
                    nxt["listener"] = t["listener"]
                log(f"music: {t.get('title')!r} would not play ({t.get('error') or 'broke off'}) - "
                    f"the next hit {nxt.get('title')!r}")
                self.queue[self.index] = nxt
                self._load()
                return
            if played < EARLY_END:
                listener_log(t, False, f"{t.get('title')!r} would not play: {t.get('error') or 'the stream broke off'}")
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
        now = {k: t.get(k) for k in ("title", "artist", "album", "year", "duration", "thumb")}
        for k in ("title", "artist", "album"):
            now[k] = clean(now[k])
        return {"type": "view", "kind": "music",
                "now": now,
                "pos": 0 if self.loading else round(self.position(), 1), "at": int(time.time() * 1000),
                "paused": self.paused, "loading": self.loading,
                "volume": volume_get(), "bpm": t.get("bpm") or 0, "first": t.get("first") or 0,
                "index": self.index + 1, "count": len(self.queue),
                "next": [{"title": clean(x.get("title")), "artist": clean(x.get("artist")), "thumb": x.get("thumb")}
                         for x in self.queue[self.index + 1:self.index + 6]]}

    def view(self):
        """The player on the TV - unless a picture view is open over it, or the
        slideshow runs (then only its line about the track changes)."""
        from core import screen
        import slideshow
        if screen.state["p"] is None:
            if slideshow.reshow():
                return
            ev = self.view_event()
            if ev:
                ui(ev)


PLAYER = Player()
