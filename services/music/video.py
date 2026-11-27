"""The media library for Eva: the hub's folders of video and music as tiles, posters cut from the files, and playback on a
DLNA renderer (a TV's own player: it decodes, the Station does nothing - tools/dlna_poc.py, 2026-11-27).

Settings: SETTINGS (json, written by the admin page «Ресурсы»):
    {"video_roots": ["/home/<user>/Videos", ...], "music_roots": ["/media"],
     "renderer": {"name": "...", "url": "http://<tv>:<port>/<desc>.xml"}}

    GET /library/list?kind=video|music[&dir=<path>] -> {"dir","parent","items":[{"name","path","folder","poster","count"}]}
    GET /library/find?kind=..&q=<words>  -> the same items: folders and files whose name holds the words
    GET /library/files?kind=..&dir=<p>  -> [path]: every file under the folder, in order (a whole album / series)
    GET /library/poster?kind=..&path=<p> -> image: a film's frame / a track's embedded cover; a folder: its
                                            cover.jpg (poster.jpg, folder.jpg) or its first file's
    GET /video/f/<token>.<ext>       -> the file itself with Range (what the renderer fetches)
    GET /dlna/renderers              -> [{"name","url"}]: MediaRenderers answering SSDP
    GET /dlna/play?path=<p>[&all=1][&renderer=<url>]   -> a film, or the folder from it on, as a series
    GET /dlna/control?action=pause|resume|stop|next|previous|seek[&to=hh:mm:ss]
    GET /dlna/state                  -> {"state","title","index","count","pos","dur","renderer"}
"""
import base64
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

SETTINGS = os.environ.get("MEDIA_SETTINGS", "/settings/media.json")
CACHE = os.environ.get("CACHE_DIR", "/cache")
POSTERS = os.path.join(CACHE, "posters")
SELF = os.environ.get("SELF_URL", "")          # how the renderer reaches this service: http://<hub>:8904
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".mpg", ".mpeg", ".ts", ".m4v", ".mov", ".wmv", ".webm")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")
EXT = {"video": VIDEO_EXT, "music": AUDIO_EXT}
MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".avi": "video/avi",
        ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".ts": "video/vnd.dlna.mpeg-tts", ".mov": "video/quicktime",
        ".wmv": "video/x-ms-wmv", ".webm": "video/webm"}
COVERS = ("poster.jpg", "cover.jpg", "folder.jpg", "poster.png", "cover.png")


def err(msg):
    print(time.strftime("%H:%M:%S"), "video:", msg, file=sys.stderr, flush=True)


def settings():
    try:
        return json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def roots(kind="video"):
    s = settings()
    r = s.get(f"{kind}_roots") or ([] if kind == "video" else ["/media"])
    return [os.path.realpath(x) for x in r if os.path.isdir(x)]


def inside(path, kind="video"):
    """The path, real, if it lies under one of the roots - else None (nothing outside is served)."""
    if not path:
        return None
    p = os.path.realpath(path)
    return p if any(p == r or p.startswith(r + os.sep) for r in roots(kind)) else None


def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _films(d, kind="video"):
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return sorted((n for n in names if n.lower().endswith(EXT[kind]) and not n.startswith(".")), key=_natural)


def _count(d, kind="video", depth=3):
    n = len(_films(d, kind))
    if depth:
        try:
            for x in os.scandir(d):
                if x.is_dir() and not x.name.startswith("."):
                    n += _count(x.path, kind, depth - 1)
        except OSError:
            pass
    return n


def _item(path, folder, kind):
    name = os.path.basename(path)
    it = {"name": name if folder else os.path.splitext(name)[0], "path": path, "folder": folder,
          "poster": f"/library/poster?kind={kind}&path=" + urllib.parse.quote(path)}
    if folder:
        it["count"] = _count(path, kind)
    return it


def listing(d=None, kind="video"):
    if not d:
        # the roots that hold something; just one - straight into it (two tiles, one of them an empty
        # torrents folder, made «открой первую» a needless step, 2026-11-27)
        full = [r for r in roots(kind) if _count(r, kind)]
        if len(full) == 1:
            return listing(full[0], kind)
        return {"kind": kind, "dir": "", "parent": None, "items": [_item(r, True, kind) for r in full]}
    p = inside(d, kind)
    if not p or not os.path.isdir(p):
        return {"error": "нет такой папки"}
    dirs = sorted((x.path for x in os.scandir(p) if x.is_dir() and not x.name.startswith(".")
                   and _count(x.path, kind)), key=lambda s: _natural(os.path.basename(s)))
    items = [_item(x, True, kind) for x in dirs] + [_item(os.path.join(p, f), False, kind) for f in _films(p, kind)]
    parent = "" if p in roots(kind) else os.path.dirname(p)
    # warm the tiles' posters in the background (a film's frame takes ~1 s to cut)
    threading.Thread(target=lambda: [poster(i["path"], kind) for i in items[:40]], daemon=True).start()
    return {"kind": kind, "dir": p, "parent": parent, "items": items}


def files(d, kind="video"):
    p = inside(d, kind)
    if not p or not os.path.isdir(p):
        raise ValueError("нет такой папки")
    return [os.path.join(r, f) for r, ds, fs in sorted(os.walk(p), key=lambda t: _natural(t[0]))
            for f in sorted((f for f in fs if f.lower().endswith(EXT[kind]) and not f.startswith(".")), key=_natural)]


def find(q, kind="video"):
    words = [w for w in q.lower().split() if w]
    out = []
    for r in roots(kind):
        for root, dirs, files in os.walk(r):
            dirs[:] = sorted((x for x in dirs if not x.startswith(".")), key=_natural)
            for x in dirs + sorted((f for f in files if f.lower().endswith(EXT[kind])), key=_natural):
                if all(w in x.lower() for w in words):
                    p = os.path.join(root, x)
                    if os.path.isdir(p) and not _count(p, kind):
                        continue
                    out.append(_item(p, os.path.isdir(p), kind))
            if len(out) > 40:
                break
    return {"kind": kind, "dir": None, "parent": None, "query": q, "items": out[:40]}


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=30)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


_plock = threading.Lock()
_pbusy = {}


def poster(path, kind="video"):
    """A jpg for the tile: the folder's own cover, else a frame ~10% into the film (titles skipped,
    at most 10 min in) or a track's embedded cover; a folder without a cover shows its first file's.
    -> file or None."""
    p = inside(path, kind)
    if not p:
        return None
    if os.path.isdir(p):
        names = {n.lower(): n for n in os.listdir(p)}
        for c in COVERS:
            if c in names:
                return os.path.join(p, names[c])
        for f in _films(p, kind)[:3]:
            got = poster(os.path.join(p, f), kind)
            if got:
                return got
        for x in sorted(os.scandir(p), key=lambda e: _natural(e.name)):
            if x.is_dir() and _count(x.path, kind):
                return poster(x.path, kind)
        return None
    base = os.path.splitext(p)[0]
    for c in (base + ".jpg", base + ".png", base + "-poster.jpg"):
        if os.path.isfile(c):
            return c
    if kind == "music":
        d = os.path.dirname(p)
        names = {n.lower(): n for n in os.listdir(d)}
        for c in COVERS:
            if c in names:
                return os.path.join(d, names[c])
    out = os.path.join(POSTERS, hashlib.sha1(p.encode()).hexdigest() + ".jpg")
    if os.path.isfile(out):
        return out
    with _plock:
        ev = _pbusy.get(out)
        mine = ev is None
        if mine:
            ev = _pbusy[out] = threading.Event()
    if not mine:
        ev.wait(60)
        return out if os.path.isfile(out) else None
    try:
        os.makedirs(POSTERS, exist_ok=True)
        tmp = out + ".tmp.jpg"
        if kind == "music":       # the cover embedded in the track, if it has one
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", p, "-map", "0:v:0", "-frames:v", "1",
                            "-vf", "scale=480:-2", "-q:v", "4", tmp], capture_output=True, timeout=30)
        else:
            at = min(600.0, max(5.0, duration(p) * 0.1))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.0f}", "-i", p, "-frames:v", "1",
                            "-vf", "scale=480:-2", "-q:v", "4", tmp], capture_output=True, timeout=60)
        if os.path.isfile(tmp) and os.path.getsize(tmp) > 1000:
            os.replace(tmp, out)
        return out if os.path.isfile(out) else None
    except (OSError, subprocess.SubprocessError) as e:
        err(f"poster {p}: {e}")
        return None
    finally:
        with _plock:
            _pbusy.pop(out, None)
        ev.set()


def token(path):
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def untoken(t):
    t = t.split(".")[0]
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)).decode()


def _self(renderer):
    """This service as the renderer reaches it: SELF_URL, else the hub's own address on the way to it."""
    if SELF:
        return SELF
    host = urllib.parse.urlparse(renderer).hostname
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return f"http://{s.getsockname()[0]}:{os.environ.get('PORT', '8904')}"
    finally:
        s.close()


def file_url(path, renderer):
    return f"{_self(renderer)}/video/f/{token(path)}{os.path.splitext(path)[1].lower()}"


def serve_file(h, tok):
    """The film with Range: the TV seeks and resumes by byte ranges."""
    try:
        p = inside(untoken(tok))
    except (ValueError, UnicodeDecodeError):
        p = None
    if not p or not os.path.isfile(p):
        return h._json(404, {"error": "нет такого файла"})
    size = os.path.getsize(p)
    start, end = 0, size - 1
    m = re.match(r"bytes=(\d*)-(\d*)", h.headers.get("Range", ""))
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:
            start = size - int(m.group(2))
        h.send_response(206)
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    else:
        h.send_response(200)
    mime = MIME.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
    h.send_header("Content-Type", mime)
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(end - start + 1))
    h.send_header("transferMode.dlna.org", "Streaming")
    h.send_header("contentFeatures.dlna.org", "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
    h.end_headers()
    if h.command == "HEAD":
        return
    left = end - start + 1
    try:
        with open(p, "rb") as f:
            f.seek(start)
            while left > 0:
                b = f.read(min(262144, left))
                if not b:
                    break
                h.wfile.write(b)
                left -= len(b)
    except (BrokenPipeError, ConnectionResetError):
        pass


# ---- DLNA: discovery and AVTransport ----
def renderers(wait=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    for _ in range(2):
        s.sendto(msg, ("239.255.255.250", 1900))
    locs, end = set(), time.time() + wait
    while time.time() < end:
        try:
            d, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        m = re.search(r"(?im)^location:\s*(\S+)", d.decode(errors="ignore"))
        if m:
            locs.add(m.group(1))
    out = []
    for loc in sorted(locs):
        try:
            x = urllib.request.urlopen(loc, timeout=4).read().decode(errors="replace")
        except OSError:
            continue
        name = (re.search(r"<friendlyName>(.*?)</friendlyName>", x) or [None, loc])[1]
        out.append({"name": html.unescape(name), "url": loc})
    return out


_desc = {}


def _control(desc_url):
    """The renderer's AVTransport control url, read from its description."""
    if desc_url not in _desc:
        x = urllib.request.urlopen(desc_url, timeout=5).read().decode(errors="replace")
        for svc in re.findall(r"<service>(.*?)</service>", x, re.S):
            if "AVTransport" in svc:
                c = re.search(r"<controlURL>(.*?)</controlURL>", svc).group(1).strip()
                _desc[desc_url] = urllib.parse.urljoin(desc_url, c)
    return _desc[desc_url]


def soap(desc_url, action, args=""):
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID>{args}'
            f'</u:{action}></s:Body></s:Envelope>')
    req = urllib.request.Request(_control(desc_url), data=body.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode(errors="replace")


def _tag(x, n):
    m = re.search(f"<{n}>(.*?)</{n}>", x, re.S)
    return m.group(1) if m else None


def _secs(t):
    try:
        h, m, s = (t or "0:0:0").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return 0.0


S = {"renderer": None, "queue": [], "index": 0, "state": "idle", "pos": 0.0, "dur": 0.0, "seen_play": False,
     "stopped_by_us": False, "at": 0.0}
_slock = threading.Lock()


def _renderer(url=None):
    r = url or (settings().get("renderer") or {}).get("url")
    if not r:
        raise ValueError("не выбран DLNA-плеер (админка, «Ресурсы»)")
    return r


def _start(i):
    p = S["queue"][i]
    url = file_url(p, S["renderer"])
    title = os.path.splitext(os.path.basename(p))[0]
    mime = MIME.get(os.path.splitext(p)[1].lower(), "video/x-matroska")
    didl = (f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1" parentID="0" restricted="1">'
            f'<dc:title>{escape(title)}</dc:title><upnp:class>object.item.videoItem.movie</upnp:class>'
            f'<res protocolInfo="http-get:*:{mime}:*">{escape(url)}</res></item></DIDL-Lite>')
    soap(S["renderer"], "SetAVTransportURI", f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>")
    soap(S["renderer"], "Play", "<Speed>1</Speed>")
    S.update(index=i, state="starting", pos=0.0, dur=duration(p), seen_play=False, stopped_by_us=False, at=time.time())
    err(f"play {i + 1}/{len(S['queue'])}: {p}")


def play(path, all_=False, renderer=None):
    p = inside(path)
    if not p:
        raise ValueError("нет такого файла или папки")
    with _slock:
        if os.path.isdir(p):
            q = files(p)            # a series by seasons too: everything under it, in order
            i = 0
        elif all_:
            d = os.path.dirname(p)
            q = [os.path.join(d, f) for f in _films(d)]
            i = q.index(p)
        else:
            q, i = [p], 0
        if not q:
            raise ValueError("в папке нет видео")
        S.update(renderer=_renderer(renderer), queue=q)
        _start(i)
    return state()


def control(action, to=None):
    with _slock:
        if not S["renderer"] or not S["queue"]:
            return {"error": "видео не играет"}
        if action == "pause":
            soap(S["renderer"], "Pause")
        elif action == "resume":
            soap(S["renderer"], "Play", "<Speed>1</Speed>")
        elif action == "stop":
            S["stopped_by_us"] = True
            soap(S["renderer"], "Stop")
            S.update(queue=[], state="idle")
        elif action in ("next", "previous"):
            i = S["index"] + (1 if action == "next" else -1)
            if not 0 <= i < len(S["queue"]):
                return {"error": "дальше в очереди ничего нет" if action == "next" else "это первое видео"}
            _start(i)
        elif action == "seek" and to:
            soap(S["renderer"], "Seek", f"<Unit>REL_TIME</Unit><Target>{to}</Target>")
        else:
            return {"error": f"не знаю действия {action}"}
    return state()


def state():
    q = S["queue"]
    cur = q[S["index"]] if q else None
    return {"state": S["state"], "title": os.path.splitext(os.path.basename(cur))[0] if cur else None,
            "path": cur, "index": S["index"] + 1 if q else 0, "count": len(q), "pos": round(S["pos"]),
            "dur": round(S["dur"]), "renderer": S["renderer"]}


def watch():
    """The series goes on: a film the TV played to its end (or near it) starts the next one; a stop
    in the middle (the TV's remote) ends the queue."""
    while True:
        time.sleep(3)
        with _slock:
            if not S["renderer"] or not S["queue"]:
                continue
            try:
                st = _tag(soap(S["renderer"], "GetTransportInfo"), "CurrentTransportState") or "?"
                pi = soap(S["renderer"], "GetPositionInfo")
            except OSError as e:
                err(f"renderer: {e}")
                continue
            pos = _secs(_tag(pi, "RelTime"))
            if st in ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"):
                S["seen_play"] = S["seen_play"] or st == "PLAYING"
                S["state"] = {"PLAYING": "playing", "PAUSED_PLAYBACK": "paused"}.get(st, "starting")
                if pos:
                    S["pos"] = pos
                continue
            if st in ("STOPPED", "NO_MEDIA_PRESENT") and S["seen_play"] and not S["stopped_by_us"]:
                dur = S["dur"] or _secs(_tag(pi, "TrackDuration"))
                ended = dur and S["pos"] >= dur - 90
                if ended and S["index"] + 1 < len(S["queue"]):
                    try:
                        _start(S["index"] + 1)
                    except OSError as e:
                        err(f"next: {e}")
                    continue
                err(f"{'finished' if ended else 'stopped on the TV'} at {S['pos']:.0f}/{dur:.0f} s")
                S.update(queue=[], state="idle")
            elif st in ("STOPPED", "NO_MEDIA_PRESENT") and not S["seen_play"] and time.time() - S["at"] > 30:
                err(f"the renderer did not start ({st})")
                S.update(queue=[], state="idle")


def route(h, path, q):
    """music_server's GET for /video/* and /dlna/*. -> True if handled."""
    try:
        kind = q.get("kind") if q.get("kind") in EXT else "video"
        if path == "/library/list":
            return h._json(200, listing(q.get("dir"), kind)) or True
        if path == "/library/files":
            return h._json(200, files(q.get("dir", ""), kind)) or True
        if path == "/library/find":
            return h._json(200, find(q.get("q", ""), kind)) or True
        if path == "/library/poster":
            f = poster(q.get("path", ""), kind)
            if not f:
                return h._json(404, {"error": "нет постера"}) or True
            b = open(f, "rb").read()
            h.send_response(200)
            h.send_header("Content-Type", "image/png" if f.lower().endswith(".png") else "image/jpeg")
            h.send_header("Content-Length", str(len(b)))
            h.send_header("Cache-Control", "max-age=86400")
            h.end_headers()
            h.wfile.write(b)
            return True
        if path.startswith("/video/f/"):
            serve_file(h, path[len("/video/f/"):])
            return True
        if path == "/dlna/renderers":
            return h._json(200, renderers()) or True
        if path == "/dlna/play":
            return h._json(200, play(q.get("path", ""), q.get("all") in ("1", "true"), q.get("renderer"))) or True
        if path == "/dlna/control":
            return h._json(200, control(q.get("action", ""), q.get("to"))) or True
        if path == "/dlna/state":
            return h._json(200, state()) or True
    except (ValueError, OSError) as e:
        err(f"{path} {q}: {e!r}")
        return h._json(400, {"error": str(e)[:300]}) or True
    return False


threading.Thread(target=watch, daemon=True).start()
