#!/usr/bin/env python3
"""Music for Eva (shn-linux): search YouTube Music and stream a track as MP3.

The Station has no room for yt-dlp + ffmpeg (2 GB root, 96% full), so the heavy
part lives here and the Station only plays the stream with mpg123.

    GET /search?q=...&kind=track|album&limit=N -> [{"title","artist","duration","thumb","url"}]
    GET /info?url=<watch url>                   -> {"title","artist","album","year","duration","thumb"}
    GET /stream?url=<watch url>                 -> audio/mpeg, 48 kHz, as it is decoded
    GET /beats?url=<watch url>                  -> {"bpm","first"}: tempo and first beat (Eva dances)
    GET /prefetch?url=<watch url>               -> {"cached"}: download into the cache now (a listener's track)
    GET /media                                  -> [{"path","bytes"}]: the local files
    GET /health
A "url" may also be a direct link to an audio file (played by ffmpeg, no yt-dlp) or a local
file "/media/<name>" (~/eva-data/media on the hub). A YouTube track played once is cached as
mp3: YouTube took ~9 s from the command to the first sound, a listener's song wants it at once
(2026-11-27).
Ported from the old assistant.py (YouTube Music via yt-dlp -> ffmpeg -> mpg123).
"""
import hashlib
import json
import os
import re
import threading
import unicodedata
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8904
MEDIA = os.environ.get("MEDIA_DIR", "/media")
CACHE = os.environ.get("CACHE_DIR", "/cache")
CACHE_MAX = float(os.environ.get("CACHE_MAX", 3e9))     # bytes; the least recently played go first
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")
FFMPEG_OUT = ["-vn", "-ar", "48000", "-ac", "2", "-f", "mp3", "-b:a", "160k", "pipe:1"]


def _cached(url):
    return os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".mp3")


def source(url):
    """-> ("file", path) | ("direct", url) | ("ytdlp", url) | (None, why)."""
    if url.startswith("/media/"):
        p = os.path.realpath(os.path.join(MEDIA, url[len("/media/"):]))
        if not p.startswith(os.path.realpath(MEDIA) + os.sep) or not os.path.isfile(p):
            return None, "нет такого файла"
        return "file", p
    if not url.startswith(("https://", "http://")):
        return None, "url"
    if os.path.isfile(_cached(url)):
        return "cached", _cached(url)
    if urllib.parse.urlparse(url).path.lower().endswith(AUDIO_EXT):
        return "direct", url
    return "ytdlp", url


def decoder(url, extra_in=(), out=None):
    """The decoding pipeline for a url: (yt-dlp process or None, ffmpeg process) with ffmpeg's
    stdout piped. `out`: ffmpeg's output args."""
    kind, where = source(url)
    if kind is None:
        raise ValueError(where)
    dl = None
    if kind == "ytdlp":
        dl = subprocess.Popen(["yt-dlp", "-q", "--no-warnings", "-f", "bestaudio/best", "--no-playlist", "-o", "-", url],
                              stdout=subprocess.PIPE, stderr=sys.stderr)
        inp = ["-i", "pipe:0"]
    else:
        inp = ["-i", where]
    ff = subprocess.Popen(["ffmpeg", "-v", "error", *extra_in, *inp, *(out or FFMPEG_OUT)],
                          stdin=dl.stdout if dl else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=sys.stderr)
    if dl:
        dl.stdout.close()
    return dl, ff


def _prune():
    try:
        files = sorted((os.path.getatime(p), os.path.getsize(p), p) for p in
                       (os.path.join(CACHE, f) for f in os.listdir(CACHE) if f.endswith(".mp3")))
    except OSError:
        return
    total = sum(sz for _, sz, _ in files)
    for _, sz, p in files:
        if total <= CACHE_MAX:
            break
        os.remove(p)
        total -= sz


def prefetch(url):
    """A YouTube track into the cache, whole (a background download)."""
    kind, _ = source(url)
    if kind != "ytdlp":
        return kind in ("cached", "file", "direct")
    os.makedirs(CACHE, exist_ok=True)
    tmp = _cached(url) + f".{threading.get_ident()}.part"
    dl, ff = decoder(url)
    with open(tmp, "wb") as fo:
        while chunk := ff.stdout.read(65536):
            fo.write(chunk)
    ok = ff.wait() == 0 and (dl.wait() == 0 if dl else True) and os.path.getsize(tmp) > 50000
    if ok:
        os.replace(tmp, _cached(url))
        _prune()
        err(f"cached {url} ({os.path.getsize(_cached(url)) // 1024} KB)")
    else:
        os.remove(tmp)
    return ok


def err(msg):
    """Into the container's log: a track that would not play left no trace (2026-11-27)."""
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def _thumb(e):
    th = e.get("thumbnails") or []
    best = max(th, key=lambda t: (t.get("width") or 0), default=None) if th else None
    return (best or {}).get("url") or e.get("thumbnail")


def search(q, kind="track", limit=5):
    if kind == "album":
        # a whole album: the longest "full album" video on YouTube
        r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", f"ytsearch5:{q} full album"],
                           capture_output=True, text=True, timeout=90)
        entries = [e for e in (json.loads(r.stdout or "{}").get("entries") or []) if (e.get("duration") or 0) > 1200]
        words = {w for w in re.findall(r"\w+", q.lower()) if len(w) > 2}
        # the album asked for: most of its words in the title (the longest video was another album)
        entries.sort(key=lambda e: (len(words & set(re.findall(r"\w+", unicodedata.normalize("NFKD", e.get("title") or "")
                                                                    .encode("ascii", "ignore").decode().lower()))),
                                    e.get("duration") or 0), reverse=True)
        entries = entries[:1]
    else:
        url = "https://music.youtube.com/search?q=" + urllib.parse.quote(q)
        r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", url], capture_output=True, text=True, timeout=120)
        entries = [e for e in (json.loads(r.stdout or "{}").get("entries") or [])
                   if "watch?v=" in (e.get("url") or e.get("webpage_url") or "")]
    out = []
    for e in entries[:limit]:
        u = e.get("url") or e.get("webpage_url")
        if u and not u.startswith("http"):
            u = "https://www.youtube.com/watch?v=" + u
        artist = e.get("artist") or ", ".join(e.get("artists") or []) or e.get("channel") or e.get("uploader")
        vid = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("v", [""])[0]
        out.append({"title": e.get("title"), "artist": artist, "duration": e.get("duration"),
                    "thumb": _thumb(e) or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None), "url": u})
    return out


def probe(where):
    """A file or a direct link: its facts by ffprobe."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", where],
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        return {"error": (r.stderr.strip().splitlines() or [f"ffprobe {r.returncode}"])[-1][:200]}
    fm = json.loads(r.stdout or "{}").get("format", {})
    tags = {k.lower(): v for k, v in (fm.get("tags") or {}).items()}
    name = os.path.splitext(os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(where).path)))[0]
    return {"title": tags.get("title") or name, "artist": tags.get("artist"), "album": tags.get("album"),
            "duration": round(float(fm["duration"]), 1) if fm.get("duration") else None, "error": None}


def info(url):
    """Full facts of one track (the flat search has no artist, length, cover)."""
    kind, where = source(url)
    if kind is None:
        return {"url": url, "error": where}
    if kind in ("file", "direct"):
        return dict(probe(where), url=url)
    meta = _cached(url)[:-4] + ".json"
    if os.path.isfile(meta):
        try:
            return json.load(open(meta, encoding="utf-8"))
        except (OSError, ValueError):
            pass
    r = subprocess.run(["yt-dlp", "-J", "--no-playlist", "--skip-download", url],
                       capture_output=True, text=True, timeout=60)
    why = None
    if r.returncode:
        err(f"info {url}: yt-dlp {r.returncode}: {r.stderr.strip()[-400:]}")
        # its last ERROR line ("This video is not available") - the brain shows it as the reason
        lines = [l for l in r.stderr.splitlines() if l.startswith("ERROR:")]
        why = re.sub(r"^ERROR:\s*(\[\w+\]\s*[\w-]+:\s*)?", "", lines[-1])[:200] if lines else f"yt-dlp {r.returncode}"
    j = json.loads(r.stdout or "{}") or {}        # a failed yt-dlp prints "null"
    out = {"title": j.get("track") or j.get("title"), "artist": j.get("artist") or j.get("uploader"),
           "album": j.get("album"), "year": j.get("release_year"), "duration": j.get("duration"),
           "thumb": j.get("thumbnail"), "url": url, "error": why}
    if not why:
        os.makedirs(CACHE, exist_ok=True)
        with open(meta, "w", encoding="utf-8") as fo:
            json.dump(out, fo, ensure_ascii=False)
    return out


BEATS = {}             # url -> {"bpm", "first"}: a track is measured once


def beats(url):
    """Tempo and the first beat of a track, for Eva's dance on the TV: the
    first two minutes decoded to 11 kHz mono, spectral flux as the onset
    curve, its autocorrelation for the tempo (60-200 BPM, leaning to ~120),
    a comb over the onsets for the phase. bpm 0: no steady beat found."""
    import numpy as np
    if url in BEATS:
        return BEATS[url]
    sr, n, hop = 11025, 1024, 256
    dl, ff = decoder(url, out=["-t", "120", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1"])
    pcm, _ = ff.communicate(timeout=120)
    if dl:
        dl.kill()
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) < sr * 10:
        return {"bpm": 0, "first": 0}
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n).astype(np.float32)
    mag = np.log1p(100 * np.abs(np.fft.rfft(frames, axis=1)))
    o = np.maximum(0, np.diff(mag, axis=0)).sum(axis=1)
    fps = sr / hop
    w = int(fps * 0.5)
    o = np.maximum(0, o - np.convolve(o, np.ones(w) / w, mode="same"))   # onsets above the local level
    o = o / (o.max() + 1e-9)
    ac = np.correlate(o, o, mode="full")[len(o) - 1:]
    lags = np.arange(int(fps * 60 / 200), int(fps * 60 / 60) + 1)
    bpms = 60 * fps / lags
    score = ac[lags] * np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)
    i = int(np.argmax(score))
    if 0 < i < len(lags) - 1:                    # parabolic refinement of the lag
        a, b, c = score[i - 1], score[i], score[i + 1]
        lag = lags[i] + 0.5 * (a - c) / (a - 2 * b + c + 1e-12)
    else:
        lag = float(lags[i])
    strong = ac[lags[i]] / (np.mean(ac[lags]) + 1e-9)
    bpm = 60 * fps / lag
    # the phase: the offset whose comb of beats lands on the most onsets
    ks = np.arange(0, len(o) - lag, lag)
    phis = np.arange(0, int(lag) + 1)
    comb = [o[np.minimum(len(o) - 1, np.round(p + ks).astype(int))].sum() for p in phis]
    first = float(phis[int(np.argmax(comb))]) / fps + n / 2 / sr
    res = {"bpm": round(float(bpm), 2) if strong > 1.15 else 0, "first": round(first, 3), "strength": round(float(strong), 2)}
    BEATS[url] = res
    return res


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/health":
            return self._json(200, {"ok": True})
        if u.path == "/search":
            try:
                return self._json(200, search(q.get("q", ""), q.get("kind", "track"), int(q.get("limit", 5))))
            except Exception as e:
                err(f"{u.path} {q}: {e!r}")
                return self._json(500, {"error": str(e)[:300]})
        if u.path == "/info":
            try:
                return self._json(200, info(q.get("url", "")))
            except Exception as e:
                err(f"{u.path} {q}: {e!r}")
                return self._json(500, {"error": str(e)[:300]})
        if u.path == "/beats":
            try:
                return self._json(200, beats(q.get("url", "")))
            except Exception as e:
                err(f"{u.path} {q}: {e!r}")
                return self._json(500, {"error": str(e)[:300]})
        if u.path == "/prefetch":
            url = q.get("url", "")
            threading.Thread(target=lambda: prefetch(url) if source(url)[0] == "ytdlp" else None, daemon=True).start()
            return self._json(200, {"cached": source(url)[0] in ("cached", "file", "direct")})
        if u.path == "/media":
            out = []
            for root, _, files in os.walk(MEDIA):
                for fn in files:
                    if fn.lower().endswith(AUDIO_EXT):
                        p = os.path.join(root, fn)
                        out.append({"path": "/media/" + os.path.relpath(p, MEDIA).replace(os.sep, "/"),
                                    "bytes": os.path.getsize(p)})
            return self._json(200, sorted(out, key=lambda x: x["path"]))
        if u.path == "/stream":
            url = q.get("url", "")
            kind, where = source(url)
            if kind is None:
                return self._json(400, {"error": where})
            # their errors (quiet otherwise: -q, -v error) go to the container's log
            err(f"stream {kind} {url}")
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            if kind == "cached":
                # our own mp3, as it was made: no decoding, the sound starts at once
                os.utime(where)
                try:
                    with open(where, "rb") as fi:
                        while chunk := fi.read(65536):
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            dl, ff = decoder(url)
            # a YouTube track played to its end stays in the cache (a skip leaves nothing)
            tmp = _cached(url) + f".{threading.get_ident()}.part" if kind == "ytdlp" else None
            fo = None
            if tmp:
                os.makedirs(CACHE, exist_ok=True)
                fo = open(tmp, "wb")
            whole = False
            try:
                while True:
                    chunk = ff.stdout.read(16384)
                    if not chunk:
                        whole = ff.wait() == 0
                        break
                    if fo:
                        fo.write(chunk)
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass          # the Station stopped or skipped the track
            finally:
                for p in (ff, dl):
                    if p is not None and p.poll() is None:
                        p.kill()
                if fo:
                    fo.close()
                    if whole and os.path.getsize(tmp) > 50000:
                        os.replace(tmp, _cached(url))
                        _prune()
                        err(f"cached {url}")
                    else:
                        os.remove(tmp)
            return
        self._json(404, {"error": "not found"})


def self_update(every=24 * 3600):
    """yt-dlp and its challenge solver follow YouTube's player changes almost weekly; a month-old
    copy failed on a song the family wanted (2026-11-27). Each call runs the yt-dlp binary anew,
    so an update takes effect without a restart."""
    while True:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "-U",
                            "yt-dlp[default]", "yt-dlp-ejs"], capture_output=True, text=True, timeout=600)
        v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
        err(f"yt-dlp {v}" + (f" (update failed: {r.stderr.strip()[-200:]})" if r.returncode else ""))
        time.sleep(every)


if __name__ == "__main__":
    import threading
    threading.Thread(target=self_update, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
