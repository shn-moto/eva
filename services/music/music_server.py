#!/usr/bin/env python3
"""Music for Eva (shn-linux): search YouTube Music and stream a track as MP3.

The Station has no room for yt-dlp + ffmpeg (2 GB root, 96% full), so the heavy
part lives here and the Station only plays the stream with mpg123.

    GET /search?q=...&kind=track|album&limit=N -> [{"title","artist","duration","thumb","url"}]
    GET /info?url=<watch url>                   -> {"title","artist","album","year","duration","thumb"}
    GET /stream?url=<watch url>                 -> audio/mpeg, 48 kHz, as it is decoded
    GET /beats?url=<watch url>                  -> {"bpm","first"}: tempo and first beat (Eva dances)
    GET /health
Ported from the old assistant.py (YouTube Music via yt-dlp -> ffmpeg -> mpg123).
"""
import json
import re
import unicodedata
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8904


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


def info(url):
    """Full facts of one track (the flat search has no artist, length, cover)."""
    r = subprocess.run(["yt-dlp", "-J", "--no-playlist", "--skip-download", url],
                       capture_output=True, text=True, timeout=60)
    why = None
    if r.returncode:
        err(f"info {url}: yt-dlp {r.returncode}: {r.stderr.strip()[-400:]}")
        # its last ERROR line ("This video is not available") - the brain shows it as the reason
        lines = [l for l in r.stderr.splitlines() if l.startswith("ERROR:")]
        why = re.sub(r"^ERROR:\s*(\[\w+\]\s*[\w-]+:\s*)?", "", lines[-1])[:200] if lines else f"yt-dlp {r.returncode}"
    j = json.loads(r.stdout or "{}") or {}        # a failed yt-dlp prints "null"
    return {"title": j.get("track") or j.get("title"), "artist": j.get("artist") or j.get("uploader"),
            "album": j.get("album"), "year": j.get("release_year"), "duration": j.get("duration"),
            "thumb": j.get("thumbnail"), "url": url, "error": why}


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
    dl = subprocess.Popen(["yt-dlp", "-q", "-f", "bestaudio", "--no-playlist", "-o", "-", url],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-t", "120", "-vn", "-ac", "1", "-ar", str(sr),
                         "-f", "s16le", "pipe:1"], stdin=dl.stdout, capture_output=True, timeout=120)
    dl.kill()
    x = np.frombuffer(ff.stdout, dtype=np.int16).astype(np.float32) / 32768.0
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
        if u.path == "/stream":
            url = q.get("url", "")
            if not url.startswith("https://"):
                return self._json(400, {"error": "url"})
            # their errors (quiet otherwise: -q, -v error) go to the container's log
            err(f"stream {url}")
            dl = subprocess.Popen(["yt-dlp", "-q", "--no-warnings", "-f", "bestaudio", "--no-playlist", "-o", "-", url],
                                  stdout=subprocess.PIPE, stderr=sys.stderr)
            ff = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "pipe:0", "-vn", "-ar", "48000", "-ac", "2",
                                   "-f", "mp3", "-b:a", "160k", "pipe:1"],
                                  stdin=dl.stdout, stdout=subprocess.PIPE, stderr=sys.stderr)
            dl.stdout.close()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            try:
                while True:
                    chunk = ff.stdout.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass          # the Station stopped or skipped the track
            finally:
                for p in (ff, dl):
                    if p.poll() is None:
                        p.kill()
            return
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
