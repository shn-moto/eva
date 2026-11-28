"""Eva's eyes on the hub: the home's cameras - motion, faces, who it is.

The cameras are in EYES_DIR/eyes.json, edited on the admin page «Глаза»:

    {"cameras": [{"id": "cam1", "name": "кабинет", "url": "http://<cam>:8080/stream", "on": true,
                  "motion": 0.01, "faces": true}],
     "fps": 5, "quiet_s": 8, "id_threshold": 0.5, "id_floor": 0.45, "min_face": 28}

    url      MJPEG over HTTP (multipart/x-mixed-replace), a JPEG snapshot URL (polled), rtsp://, or
             gopro://<ip> (a GoPro on its own Wi-Fi: preview started over its HTTP API, UDP 8554)
    motion   the changed part of the frame that counts as motion (0.01 = 1 %)
    watch    track motion (false: the live frame only, no events)
    faces    look for faces during motion and ask photolib who it is
    motion_on / faces_on (top level): the same two switches for all the cameras

Per camera: a reader thread keeps only the newest frame (no decode), an analyser decodes it
FPS times a second at 1/4 size (libjpeg scales while decoding - cheap) and compares it with
a running background. Motion opens an event; while it lasts, a face pass every FACE_EVERY s
(YuNet on the CPU, no VRAM - the GPU is full: photolib + router) crops the faces, and each
logged face goes to photolib's /search/face: the family's faces are already there, named.
A face keeps an event open only after motion within the last minute: a photo on the wall
must not keep a camera "busy" forever.

    GET /health
    GET /cameras                 state: online, fps, motion now, the open event, who is seen
    GET /frame/<id>.jpg?boxes=1  the newest frame, motion and faces drawn
    GET /presence?minutes=10     who was seen where (from the database)
    GET /journal?hours=2         events of a period with who was recognised (camera=, person=, since=, until=)
    GET /probe?url=              one frame from a URL (the admin page's «Проверить»)
    GET /log/<id>                a camera's health log (reconnects, stalls, lag, photolib errors)
    GET /events                  SSE: motion (an event opens) / active (still moving, every 10 s) / face / end

Database (assistant_memory, ASSISTANT_PG_DSN), tables made here:
    eye_events  one motion event: camera, started, ended, peak, snapshot, people
    eye_faces   a face seen in an event: crop, box, person (photolib), similarity
Files: EYES_DIR/events/<day>/<id>.jpg (the frame at the peak), faces/<day>/<id>.jpg; KEEP_DAYS.
"""
import collections
import datetime
import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
import uuid

# RTSP the live way: TCP, no demuxer buffer, low delay - a Wi-Fi camera's stall was played back late
# as if live (a face recognised after the person had left, 2026-11-28). Read before cv2 opens a stream.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000")
import cv2
import numpy as np
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

EYES_DIR = os.environ.get("EYES_DIR", "/eyes")
CONF = os.path.join(EYES_DIR, "eyes.json")
DSN = os.environ.get("ASSISTANT_PG_DSN", "")
PHOTOLIB = os.environ.get("PHOTOLIB_URL", "http://host.docker.internal:8000")
YUNET = os.environ.get("YUNET_MODEL", "/models/face_detection_yunet_2023mar.onnx")
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "30"))
FACE_EVERY = 0.7          # s between face passes during an event
IDLE_FACE_EVERY = 5.0     # s between face passes with no event (a person sitting still)
FACE_LOG_EVERY = 3.0      # s between logged faces of one event: a person at the desk is not 400 rows
QUERY_EVERY = 2.0         # s between photolib questions of one camera (~0.7 s each there)
HEALTH_WINDOW = 600        # s: problems this recent make a camera yellow
LAG_MAX = 3.0              # s an RTSP stream may fall behind real time before it is reconnected
GAP_RESET = 2.0            # s without frames: the background starts anew (no motion out of a stall)
ACTIVE_EVERY = 10.0        # s between "still moving" events of an open event (the scenarios)
RECHECK_S = 15            # a tracked face keeps its name this long, then photolib is asked again
MIN_FACE = 28             # px (eyes.json min_face): smaller faces are neither caught nor asked - photolib's
                          # answer for a 28x42 profile across the room was already borderline (2026-11-28)
TRACK_IOU = 0.3           # a face is the same face as one in the last pass when the boxes overlap this much
TRACK_GAP = 2.0           # s: a face not seen longer starts a new track (asked anew)
MAX_EVENT_S = 300         # a long presence is split into 5-minute events
RECENT_MOTION_S = 60      # a face keeps an event only this long after the last motion
LIGHT = 0.5               # more of the frame changed at once = the light, not a person
cv2.setNumThreads(2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS eye_events (
    id bigserial PRIMARY KEY, camera text NOT NULL, started timestamptz NOT NULL, ended timestamptz,
    peak real, frames int, snapshot text, people text[] NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS eye_events_started ON eye_events (started);
CREATE TABLE IF NOT EXISTS eye_faces (
    id bigserial PRIMARY KEY, event_id bigint REFERENCES eye_events(id) ON DELETE CASCADE,
    camera text NOT NULL, ts timestamptz NOT NULL, crop text, score real, box int[],
    person_id int, person text, similarity real);
CREATE INDEX IF NOT EXISTS eye_faces_ts ON eye_faces (ts);
COMMENT ON TABLE eye_events IS 'Eva''s eyes: motion events per camera (services/eyes); people = names recognised';
COMMENT ON TABLE eye_faces IS 'Eva''s eyes: faces seen during events; person from photolib, null = not recognised';
"""


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def now():
    return datetime.datetime.now().astimezone()


# ---- the database: one connection, reconnected; the eyes keep working without it ----
_db = [None]
_db_lock = threading.Lock()


def sql(q, args=(), one=False):
    if not DSN:
        return None
    with _db_lock:
        for attempt in (0, 1):
            try:
                if _db[0] is None or _db[0].closed:
                    _db[0] = psycopg.connect(DSN, autocommit=True, connect_timeout=5)
                with _db[0].cursor() as c:
                    c.execute(q, args)
                    if c.description is None:
                        return None
                    return c.fetchone() if one else c.fetchall()
            except psycopg.OperationalError as e:
                _db[0] = None
                if attempt:
                    log("db:", e)
            except Exception as e:
                log("db:", e)
                return None
    return None


def conf():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"cameras": []}
    except Exception as e:
        log("eyes.json:", e)
        return None


def _save_jpg(kind, img, quality=85):
    day = time.strftime("%Y-%m-%d")
    rel = f"{kind}/{day}/{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}.jpg"
    path = os.path.join(EYES_DIR, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return rel


def _multipart(field, name, data, ctype="image/jpeg"):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def identify(jpg, threshold, floor=None):
    """A face crop -> (person_id, name, similarity) by photolib's face base, or (None, None, best).
    A match: the top named face >= threshold, or >= floor with 3 faces of that person among the top 5.
    Measured on «детская» (28-30 px profile faces across the room, 2026-11-28): the user at 0.47-0.53,
    a miss at 0.44 - threshold 0.5, floor 0.45."""
    floor = threshold - 0.05 if floor is None else floor
    body, ctype = _multipart("file", "face.jpg", jpg)
    req = urllib.request.Request(f"{PHOTOLIB}/search/face?top_k=5&similarity_threshold=0.3",
                                 data=body, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.loads(r.read()).get("results") or []
    named = [x for x in res if x.get("person_id")]
    if not named:
        return None, None, (res[0]["similarity"] if res else None)
    best = named[0]
    # the top named face decides; a second face of the same person near it makes a weak match count
    votes = sum(1 for x in named if x["person_id"] == best["person_id"] and x["similarity"] >= floor - 0.03)
    if best["similarity"] >= threshold or (votes >= 3 and best["similarity"] >= floor):
        return best["person_id"], best.get("person_name"), round(best["similarity"], 3)
    return None, None, round(best["similarity"], 3)


# ---- events out (GET /events, SSE): the brain's camera scenarios (app/core/watchers.py) ----
_subs = []
_subs_lock = threading.Lock()


def publish(ev):
    ev = dict(ev, ts=time.time())
    with _subs_lock:
        for q in list(_subs):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


# ---- reading a camera: only the newest frame is kept, nothing decoded here ----
class Reader(threading.Thread):
    def __init__(self, url, fps):
        super().__init__(daemon=True)
        self.url, self.fps, self.running = url, fps, True
        self.frame = None          # (ts, "jpg", bytes) or (ts, "bgr", ndarray)
        self.seq = 0
        self.error = None
        self.got = []              # arrival times, for the camera's fps
        self.conn = 0              # +1 on every (re)connect: the analyser starts its background anew
        self.issues = collections.deque(maxlen=200)    # (ts, level, msg): the camera's health log
        self.down_since = None     # the first failure of an outage: one log line per outage, not per retry

    def note(self, level, msg):
        self.issues.append((time.time(), level, str(msg)[:300]))

    def put(self, kind, data):
        t = time.time()
        if self.down_since is not None:
            self.note("info", f"кадры снова идут (не было {int(t - self.down_since)} с)")
            self.down_since = None
        self.frame = (t, kind, data)
        self.seq += 1
        self.got = [x for x in self.got[-300:] if t - x < 5] + [t]      # 60 capped the shown fps at 12

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                if self.url.startswith(("rtsp://", "rtsps://")):
                    self._rtsp()
                elif self.url.startswith("gopro://"):
                    self._gopro()
                else:
                    self._http()
            except Exception as e:
                self.error = str(e)[:200]
                # an outage logged once with its first reason; a lag reconnect each time (it is the point)
                if self.down_since is None or "behind" in self.error:
                    self.note("error", self.error)
                self.down_since = self.down_since or time.time()
            if self.running:
                time.sleep(3)

    def _http(self):
        with urllib.request.urlopen(self.url, timeout=5) as r:
            ctype = r.headers.get("Content-Type", "")
            self.error = None
            self.conn += 1
            if ctype.startswith("image/"):
                self.put("jpg", r.read())
                return self._poll()
            if "multipart" not in ctype:
                raise IOError(f"not a camera stream: {ctype or 'no content type'}")
            buf = bytearray()
            while self.running:
                chunk = r.read1(65536)
                if not chunk:
                    raise IOError("stream ended")
                buf += chunk
                while True:
                    a = buf.find(b"\xff\xd8")
                    if a < 0:
                        del buf[:-1]
                        break
                    b = buf.find(b"\xff\xd9", a + 2)
                    if b < 0:
                        del buf[:a]
                        break
                    self.put("jpg", bytes(buf[a:b + 2]))
                    del buf[:b + 2]
                if len(buf) > 8_000_000:
                    buf.clear()

    def _poll(self):
        while self.running:
            time.sleep(1.0 / self.fps)
            with urllib.request.urlopen(self.url, timeout=10) as r:
                self.put("jpg", r.read())

    def _rtsp(self):
        self._capture(self.url, open_ms=5000)

    def _gopro(self):
        """A GoPro (HERO9+, Open GoPro HTTP API) on its own Wi-Fi: gopro://10.5.5.9[?port=8554]. The camera
        sends its preview as MPEG-TS over UDP to whoever asked, and stops without a keep-alive every few
        seconds (HERO11 Black: 1280x1120 at ~29 fps, 2026-11-28)."""
        u = urllib.parse.urlparse(self.url)
        port = int(urllib.parse.parse_qs(u.query).get("port", ["8554"])[0])
        api = f"http://{u.hostname}:{u.port or 8080}/gopro/camera"

        def get(path):
            with urllib.request.urlopen(api + path, timeout=5) as r:
                return r.read()
        try:
            get("/stream/stop")                # a stream left running by the last connection
        except OSError:
            pass
        try:
            get(f"/stream/start?port={port}")
        except OSError as e:
            raise IOError(f"GoPro не отвечает ({u.hostname}): {e}")
        alive = [True]

        def keep():
            while alive[0] and self.running:
                try:
                    get("/keep_alive")
                except OSError:
                    pass
                time.sleep(2.5)
        threading.Thread(target=keep, daemon=True).start()
        try:
            # UDP: the first frames take a while to probe; no lag check (TS over UDP has no backlog to replay)
            self._capture(f"udp://@0.0.0.0:{port}?overrun_nonfatal=1&fifo_size=5000000", open_ms=15000, lag=False)
        finally:
            alive[0] = False
            try:
                get("/stream/stop")
            except OSError:
                pass

    def _capture(self, src, open_ms=5000, lag=True):
        # 5 s, not FFmpeg's 30: a stalled camera was waited for 30 s at a time (2026-11-28)
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_ms,
                                                     cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
        try:
            if not cap.isOpened():
                raise IOError(f"{src.split(':')[0]}: cannot open")
            self.error = None
            self.conn += 1
            base = None                    # the smallest (wall clock - stream clock) seen: no lag
            while self.running:
                ok, img = cap.read()
                if not ok:
                    raise IOError(f"{src.split(':')[0]}: no frame")
                # how late is this frame: the stream's own clock against ours. A camera that sends
                # a backlog after a network hiccup is behind - reconnect and start from now.
                pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 if lag else 0
                if pts > 0:
                    off = time.time() - pts
                    base = off if base is None else min(base, off)
                    if off - base > LAG_MAX:
                        raise IOError(f"rtsp: {off - base:.1f} s behind the camera - reconnecting")
                self.put("bgr", img)
        finally:
            cap.release()


def decode(kind, data, reduce=1, gray=False):
    if kind == "bgr":
        img = cv2.cvtColor(data, cv2.COLOR_BGR2GRAY) if gray else data
        return cv2.resize(img, None, fx=1 / reduce, fy=1 / reduce, interpolation=cv2.INTER_AREA) if reduce > 1 else img
    flag = {(1, False): cv2.IMREAD_COLOR, (2, False): cv2.IMREAD_REDUCED_COLOR_2, (4, False): cv2.IMREAD_REDUCED_COLOR_4,
            (8, False): cv2.IMREAD_REDUCED_COLOR_8, (1, True): cv2.IMREAD_GRAYSCALE, (2, True): cv2.IMREAD_REDUCED_GRAYSCALE_2,
            (4, True): cv2.IMREAD_REDUCED_GRAYSCALE_4, (8, True): cv2.IMREAD_REDUCED_GRAYSCALE_8}[(reduce, gray)]
    return cv2.imdecode(np.frombuffer(data, np.uint8), flag)


# ---- one camera: motion, events, faces ----
class Eye(threading.Thread):
    def __init__(self, cam, g):
        super().__init__(daemon=True)
        self.cam, self.g = cam, g
        self.id = cam["id"]
        self.running = True
        self.reader = Reader(cam["url"], g.get("fps", 5))
        self.bg = None
        self.reduce = None
        self.size = None                 # (w, h) of the full frame
        self.motion = 0.0                # changed part of the frame now
        self.mbox = None                 # motion bbox (full coords)
        self.faces = []                  # [(box, score, name)] of the last face pass
        self.tracks = []                 # faces followed between passes: {box, seen, name, pid, sim, named_at, asked_at}
        self.faces_ts = 0
        self.event = None
        self.last_motion = 0
        self.last_face_pass = 0
        self.light = 0
        self.det = None
        self.det_size = None
        self.analysed = []
        self.conn_seen = 0
        self.prev_t = 0.0

    def stop(self):
        self.running = False
        self.reader.stop()

    # -- state for the page --
    def health(self):
        """green - frames come, nothing wrong in HEALTH_WINDOW; yellow - frames come, but there were
        reconnects / errors lately or the camera sends under 3 fps; red - no frames now."""
        t = time.time()
        fr = self.reader.frame
        if not (fr and t - fr[0] < 5):
            return {"level": "red", "why": self.reader.error or "нет кадров"}
        bad = [(ts, lv, m) for ts, lv, m in self.reader.issues if lv != "info" and t - ts < HEALTH_WINDOW]
        fps = len(self.reader.got) / 5
        if bad:
            return {"level": "yellow", "why": f"{len(bad)} сбоев за {HEALTH_WINDOW // 60} мин: {bad[-1][2]}"}
        if fps < 3:
            return {"level": "yellow", "why": f"мало кадров: {fps:.1f} к/с"}
        return {"level": "green", "why": f"всё хорошо · {fps:.1f} к/с"}

    def issue_log(self):
        return [{"t": datetime.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M:%S"), "level": lv, "msg": m}
                for ts, lv, m in reversed(self.reader.issues)]

    def state(self):
        t = time.time()
        fr = self.reader.frame
        online = bool(fr and t - fr[0] < 5)
        ev = self.event
        return {"id": self.id, "name": self.cam.get("name") or self.id, "online": online, "health": self.health(),
                "error": None if online else (self.reader.error or "нет кадров"),
                "fps": round(len(self.reader.got) / 5, 1), "analysed_fps": round(len([x for x in self.analysed if t - x < 5]) / 5, 1),
                "size": self.size, "motion": round(self.motion, 4), "threshold": self.cam.get("motion", 0.01),
                "event": {"id": ev["id"], "since": ev["started"].isoformat(timespec="seconds"),
                          "people": sorted(ev["people"])} if ev else None,
                "faces": [{"box": b, "score": round(s, 2), "name": n} for b, s, n in self.faces] if t - self.faces_ts < 3 else [],
                "last_motion": datetime.datetime.fromtimestamp(self.last_motion).astimezone().isoformat(timespec="seconds")
                if self.last_motion else None}

    def frame_jpg(self, boxes=True):
        fr = self.reader.frame
        if not fr:
            return None
        if not boxes and fr[1] == "jpg":
            return fr[2]
        img = decode(fr[1], fr[2]).copy()
        if boxes:
            t = time.time()
            if self.mbox and t - self.last_motion < 2:
                x1, y1, x2, y2 = self.mbox
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 255), 2)
            if t - self.faces_ts < 3:
                for (x, y, w, h), s, n in self.faces:
                    cv2.rectangle(img, (x, y), (x + w, y + h), (80, 220, 80), 2)
                    cv2.putText(img, translit(n) if n else f"? {s:.2f}", (x, max(14, y - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 220, 80), 2)
        ok, b = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return b.tobytes() if ok else None

    # -- the loop --
    def run(self):
        self.reader.start()
        seen = -1
        while self.running:
            period = 1.0 / max(1, self.g.get("fps", 5))
            t0 = time.time()
            fr, seq = self.reader.frame, self.reader.seq
            if fr and seq != seen and t0 - fr[0] < 5:
                seen = seq
                try:
                    self.step(fr)
                except Exception as e:
                    log(f"{self.id}: step:", e)
                    self.reader.note("error", f"анализ кадра: {e}")
            elif self.event and (not fr or t0 - fr[0] > 5):
                self.close_event()          # the camera went away mid-event
            time.sleep(max(0.01, period - (time.time() - t0)))
        if self.event:
            self.close_event()

    def watching(self):
        """Motion tracking on (the page's switches: all cameras, this one); off - the live frame only."""
        return self.g.get("motion_on", True) is not False and self.cam.get("watch", True) is not False

    def faces_enabled(self):
        """Faces on (the page's switches) - on their own: motion tracking off must not blind them
        (2026-11-28: «лица» ticked with «движение» off at the top, and no face was ever looked for)."""
        return self.g.get("faces_on", True) is not False and self.cam.get("faces", True) is not False

    def step(self, fr):
        t, kind, data = fr
        watch, faces_on = self.watching(), self.faces_enabled()
        if not watch and not faces_on:                 # the live frame only
            if self.event:
                self.close_event()
            self.bg, self.motion, self.faces = None, 0.0, []
            return
        # the motion is measured whenever the camera is analysed: with tracking off it opens no event,
        # but the faces still need it - a face keeps an event only after motion (a photo on the wall)
        if self.reduce is None:
            full = decode(kind, data)
            if full is None:
                return
            h, w = full.shape[:2]
            self.size = (w, h)
            self.reduce = 8 if w > 2600 else 4 if w > 1000 else 2 if w > 500 else 1
        small = decode(kind, data, self.reduce, gray=True)
        if small is None:
            return
        self.analysed = [x for x in self.analysed[-50:] if t - x < 5] + [t]
        small = cv2.GaussianBlur(small, (5, 5), 0).astype(np.float32)
        # after a reconnect or a stall the first frame differs from the old background as a whole:
        # that is not motion (events of one frame at each RTSP timeout, 2026-11-28)
        fresh = self.reader.conn != self.conn_seen or t - self.prev_t > GAP_RESET
        self.conn_seen, self.prev_t = self.reader.conn, t
        if fresh or self.bg is None or self.bg.shape != small.shape:
            self.bg = small.copy()
            return
        diff = cv2.absdiff(small, self.bg)
        mask = (diff > 22).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        frac = float(mask.mean())
        cv2.accumulateWeighted(small, self.bg, 0.05)
        thr = float(self.cam.get("motion", 0.01))
        if frac > LIGHT:                   # the light changed: start the background again
            self.bg = small.copy()
            self.light = t
            frac = 0.0
        self.motion = frac
        moving = frac >= thr
        if frac >= thr / 4:
            self.last_motion = t
        if moving:
            ys, xs = np.nonzero(mask)
            r = self.reduce
            self.mbox = (int(xs.min() * r), int(ys.min() * r), int(xs.max() * r), int(ys.max() * r))
        ev = self.event
        if moving and watch and not ev:
            self.open_event(t)
            ev = self.event
        if ev and watch:
            if moving:
                ev["last_active"] = t
                ev["frames"] += 1
                # "still moving" for the scenarios: an open event is not a new start, and a person
                # at the desk keeps one open for hours (2026-11-27: a motion scenario never fired)
                if t - ev.get("last_pub", 0) >= ACTIVE_EVERY:
                    ev["last_pub"] = t
                    publish({"type": "active", "camera": self.id, "place": self.cam.get("name") or self.id,
                             "event_id": ev["id"], "motion": round(frac, 3)})
                if frac > ev["peak"]:
                    ev["peak"], ev["peak_frame"] = frac, (kind, data)
        # faces: often during an event, rarely without one (a still person is not motion)
        want_faces = faces_on and t - self.light > 2
        every = FACE_EVERY if ev else IDLE_FACE_EVERY
        if want_faces and t - self.last_face_pass >= every and t - self.last_motion < RECENT_MOTION_S:
            self.last_face_pass = t
            found = self.face_pass(kind, data)
            if found and not self.event:
                self.open_event(t, by="face")
            if found and self.event:
                self.event["last_active"] = t
                self.log_faces(found, kind, data, t)
        ev = self.event
        if ev and (t - ev["last_active"] > self.g.get("quiet_s", 8) or t - ev["t0"] > MAX_EVENT_S):
            split = t - ev["last_active"] <= self.g.get("quiet_s", 8)
            self.close_event(split)
            if split:
                self.open_event(t, split=True)      # the same presence going on: no motion / end out

    def face_pass(self, kind, data):
        img = decode(kind, data)
        if img is None:
            return []
        h, w = img.shape[:2]
        s = min(1.0, 960 / w)
        im = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img
        ih, iw = im.shape[:2]
        if self.det is None:
            self.det = cv2.FaceDetectorYN.create(YUNET, "", (iw, ih), 0.75, 0.3, 50)
        if self.det_size != (iw, ih):
            self.det.setInputSize((iw, ih))
            self.det_size = (iw, ih)
        _, res = self.det.detect(im)
        out = []
        for f in (res if res is not None else []):
            x, y, fw, fh = (f[:4] / s).astype(int).tolist()
            if min(fw, fh) < int(self.g.get("min_face", MIN_FACE)):
                continue
            out.append(((max(0, x), max(0, y), fw, fh), float(f[14]), img))
        self.faces = [(b, sc, None) for b, sc, _ in out]
        self.faces_ts = time.time()
        return out

    def _track(self, found, t):
        """Each face of this pass -> its track: the same face as in the last pass when the boxes overlap
        (IoU) and it was seen moments ago, else a new one. A name moves only along its own track: by a
        coarse 120 px cell a child in her father's arms took his name (2026-11-28, «детская», 480x640)."""
        self.tracks = [k for k in self.tracks if t - k["seen"] <= TRACK_GAP]
        free, out = list(self.tracks), []
        for b, sc, _ in found:
            best = max(free, key=lambda k: _iou(k["box"], b), default=None)
            if best is not None and _iou(best["box"], b) >= TRACK_IOU:
                free.remove(best)
            else:
                best = {"name": None, "pid": None, "sim": None, "named_at": 0, "asked_at": 0}
                self.tracks.append(best)
            best.update(box=b, seen=t)
            out.append(best)
        return out

    def log_faces(self, found, kind, data, t):
        ev = self.event
        tracks = self._track(found, t)
        self.faces = [(b, sc, k["name"]) for (b, sc, _), k in zip(found, tracks)]
        if t - ev["last_face_log"] < FACE_LOG_EVERY:
            return
        ev["last_face_log"] = t
        if not ev["peak_frame"]:                      # an event of faces only: its frame is a face's
            ev["peak_frame"] = (kind, data)
        img = found[0][2]
        H, W = img.shape[:2]
        for ((x, y, w, h), score, _), k in zip(found, tracks):
            m = int(max(w, h) * 0.5)
            crop = img[max(0, y - m):min(H, y + h + m), max(0, x - m):min(W, x + w + m)]
            rel = _save_jpg("faces", crop, 90)
            # the same face named lately keeps its name without asking photolib: a fixed cap of 20
            # questions an event left 71 of 91 faces of a 5-minute event as «?» (2026-11-27)
            pid, name = k["pid"], k["name"]
            inherit = bool(name) and t - k["named_at"] < RECHECK_S
            row = sql("INSERT INTO eye_faces (event_id, camera, ts, crop, score, box, person_id, person) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                      (ev["id"], self.id, now(), rel, score, [x, y, w, h], pid if inherit else None,
                       name if inherit else None), one=True)
            fid = row[0] if row else None
            # each track asked at most every QUERY_EVERY s: two people in the frame are both asked
            # every caught face (>= min_face) is asked: a 28x42 face across the room had been skipped
            # by a 40 px gate and the journal showed «?» with no number (2026-11-28)
            if not inherit and t - k["asked_at"] >= QUERY_EVERY:
                k["asked_at"] = t
                ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
                if ok:
                    threading.Thread(target=self._identify, args=(ev, fid, jpg.tobytes(), k), daemon=True).start()

    def _identify(self, ev, fid, jpg, track):
        try:
            pid, name, sim = identify(jpg, float(self.g.get("id_threshold", 0.5)), float(self.g.get("id_floor", 0.45)))
        except Exception as e:
            log(f"{self.id}: photolib:", e)
            self.reader.note("warn", f"photolib не ответил: {e}")
            return
        if fid:
            sql("UPDATE eye_faces SET person_id=%s, person=%s, similarity=%s WHERE id=%s", (pid, name, sim, fid))
        publish({"type": "face", "camera": self.id, "place": self.cam.get("name") or self.id, "event_id": ev.get("id"),
                 "person": name, "similarity": sim})
        # photolib's answer is the track's name now - "nobody" too: a name inherited wrongly is dropped
        track.update(name=name, pid=pid, sim=sim, named_at=time.time() if name else 0)
        if name:
            if name not in ev["people"]:
                ev["people"].add(name)
                log(f"{self.id}: {name} ({sim})")
                if ev.get("id"):
                    sql("UPDATE eye_events SET people=%s WHERE id=%s", (sorted(ev["people"]), ev["id"]))

    def open_event(self, t, split=False, by="motion"):
        st = now()
        row = sql("INSERT INTO eye_events (camera, started) VALUES (%s,%s) RETURNING id", (self.id, st), one=True)
        self.event = {"id": row[0] if row else None, "started": st, "t0": t, "last_active": t, "frames": 0,
                      "peak": 0.0, "peak_frame": None, "people": set(), "last_face_log": 0}
        if not split and by == "motion":           # a face's event is told by its face events
            publish({"type": "motion", "camera": self.id, "place": self.cam.get("name") or self.id,
                     "event_id": self.event["id"]})

    def close_event(self, split=False):
        ev, self.event = self.event, None
        if not ev:
            return
        snap = None
        if ev["peak_frame"]:
            img = decode(*ev["peak_frame"])
            if img is not None:
                s = min(1.0, 960 / img.shape[1])
                snap = _save_jpg("events", cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img, 80)
        if ev["id"]:
            sql("UPDATE eye_events SET ended=%s, peak=%s, frames=%s, snapshot=%s, people=%s WHERE id=%s",
                (now(), ev["peak"], ev["frames"], snap, sorted(ev["people"]), ev["id"]))
        log(f"{self.id}: event {ev['id']} {int(time.time() - ev['t0'])} s, peak {ev['peak']:.3f}, {sorted(ev['people'])}")
        if not split:
            publish({"type": "end", "camera": self.id, "place": self.cam.get("name") or self.id, "event_id": ev["id"],
                     "seconds": int(time.time() - ev["t0"]), "people": sorted(ev["people"])})


def _iou(a, b):
    """Overlap of two (x, y, w, h) boxes: intersection over union, 0..1."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    return inter / float(aw * ah + bw * bh - inter) if inter else 0.0


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch  y  e yu ya".split(" ")))


def translit(s):
    """OpenCV draws ASCII only: a name on the live frame in Latin letters."""
    return "".join(_TR.get(c.lower(), c) if c.lower() in _TR else c for c in s)


# ---- the set of cameras, following eyes.json ----
EYES = {}
_conf_sig = [None]


def sync():
    c = conf()
    if c is None:
        return
    sig = json.dumps(c, sort_keys=True)
    if sig == _conf_sig[0]:
        return
    _conf_sig[0] = sig
    want = {x["id"]: x for x in c.get("cameras", []) if x.get("id") and x.get("url") and x.get("on", True)}
    for cid in list(EYES):
        e = EYES[cid]
        if cid not in want or want[cid]["url"] != e.cam["url"]:
            e.stop()
            del EYES[cid]
            log(f"{cid}: off")
        else:
            e.cam, e.g = want[cid], c            # threshold / name / faces: live
    for cid, cam in want.items():
        if cid not in EYES:
            EYES[cid] = Eye(cam, c)
            EYES[cid].start()
            log(f"{cid}: on ({cam.get('name')})")


def cleanup():
    cut = time.time() - KEEP_DAYS * 86400
    for kind in ("events", "faces"):
        root = os.path.join(EYES_DIR, kind)
        for day in os.listdir(root) if os.path.isdir(root) else []:
            try:
                if datetime.datetime.strptime(day, "%Y-%m-%d").timestamp() < cut:
                    d = os.path.join(root, day)
                    for f in os.listdir(d):
                        os.remove(os.path.join(d, f))
                    os.rmdir(d)
            except (ValueError, OSError):
                pass


def manager():
    last_clean = 0
    while True:
        try:
            sync()
            if time.time() - last_clean > 6 * 3600:
                last_clean = time.time()
                cleanup()
        except Exception as e:
            log("manager:", e)
        time.sleep(3)


app = FastAPI()


@app.on_event("startup")
def start():
    os.makedirs(EYES_DIR, exist_ok=True)
    sql(SCHEMA)
    # events left open by a restart: closed at their last face, or at the start
    sql("UPDATE eye_events e SET ended = coalesce((SELECT max(ts) FROM eye_faces f WHERE f.event_id = e.id), e.started) "
        "WHERE ended IS NULL")
    threading.Thread(target=manager, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True, "cameras": len(EYES), "online": sum(1 for e in EYES.values() if e.state()["online"])}


@app.get("/cameras")
def cameras():
    """Every camera of eyes.json - the switched-off ones too, with why: the brain's tool is always
    there and tells «выключена» itself (2026-11-28: a skill present only when a camera was on at
    the brain's start needed a restart of the brain for every switch)."""
    c = conf() or {}
    out = [dict(e.state(), watching=e.watching(), faces_enabled=e.faces_enabled()) for e in EYES.values()]
    for x in c.get("cameras", []):
        if x.get("id") not in EYES:
            out.append({"id": x.get("id"), "name": x.get("name") or x.get("id"), "online": False, "off": True,
                        "health": {"level": "off", "why": "выключена"},
                        "error": "выключена", "watching": False, "faces_enabled": False, "faces": []})
    return {"cameras": out, "motion_on": c.get("motion_on", True) is not False,
            "faces_on": c.get("faces_on", True) is not False}


@app.get("/log/{cid}")
def issue_log(cid: str):
    """A camera's health log, newest first (reconnects, stalls, lag, photolib errors)."""
    e = EYES.get(cid)
    return {"id": cid, "health": e.health() if e else {"level": "off", "why": "выключена"},
            "log": e.issue_log() if e else []}


@app.get("/frame/{cid}.jpg")
def frame(cid: str, boxes: int = 1):
    e = EYES.get(cid)
    jpg = e.frame_jpg(bool(boxes)) if e else None
    if not jpg:
        raise HTTPException(404, "no frame")
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/presence")
def presence(minutes: int = 10):
    rows = sql("SELECT camera, person, max(ts), max(similarity), count(*) FROM eye_faces "
               "WHERE ts > now() - make_interval(mins => %s) GROUP BY camera, person ORDER BY 3 DESC", (minutes,)) or []
    names = {e.id: e.cam.get("name") or e.id for e in EYES.values()}
    return {"minutes": minutes,
            "cameras": [{"id": e.id, "name": names[e.id], "motion_now": e.event is not None,
                         "last_motion": e.state()["last_motion"]} for e in EYES.values()],
            "faces": [{"camera": c, "place": names.get(c, c), "person": p, "last": ts.isoformat(timespec="seconds"),
                       "similarity": s, "count": n} for c, p, ts, s, n in rows]}


@app.get("/journal")
def journal(hours: float = 2, since: str = "", until: str = "", camera: str = "", person: str = "", limit: int = 60):
    """Events of a period with their faces, newest first, for the brain's eyes tool.
    camera: an id or a name; person: part of a name (only events where that person was recognised)."""
    names = {e.id: e.cam.get("name") or e.id for e in EYES.values()}
    for x in (conf() or {}).get("cameras", []):
        names.setdefault(x["id"], x.get("name") or x["id"])
    cam = next((i for i, n in names.items() if camera and camera.lower() in (i.lower(), n.lower())), camera)
    t1 = datetime.datetime.fromisoformat(until).astimezone() if until else now()
    t0 = datetime.datetime.fromisoformat(since).astimezone() if since else t1 - datetime.timedelta(hours=hours)
    rows = sql("SELECT e.id, e.camera, e.started, e.ended, e.peak, e.people, "
               "coalesce(json_agg(json_build_object('person', f.person, 'sim', f.similarity)) "
               "FILTER (WHERE f.id IS NOT NULL), '[]') FROM eye_events e LEFT JOIN eye_faces f ON f.event_id = e.id "
               "WHERE e.started < %s AND coalesce(e.ended, now()) > %s AND (%s = '' OR e.camera = %s) "
               "AND (%s = '' OR EXISTS (SELECT 1 FROM unnest(e.people) p WHERE p ILIKE %s)) "
               "GROUP BY e.id ORDER BY e.started DESC LIMIT %s",
               (t1, t0, cam, cam, person, f"%{person}%", min(limit, 300))) or []
    out = []
    for eid, c, st, en, peak, people, faces in rows:
        best = {}
        for f in faces:
            if f["person"] and (f["sim"] or 0) > best.get(f["person"], 0):
                best[f["person"]] = f["sim"]
        out.append({"camera": names.get(c, c), "start": st.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    "end": en.astimezone().strftime("%H:%M:%S") if en else "now",
                    "motion_peak": round(peak or 0, 3), "people": {p: round(s, 2) for p, s in best.items()},
                    "faces": len(faces), "unrecognised_faces": sum(1 for f in faces if not f["person"])})
    return {"from": t0.isoformat(timespec="minutes"), "to": t1.isoformat(timespec="minutes"), "events": out}


@app.get("/events")
def events():
    """The eyes' events as they happen (text/event-stream): motion (an event opens), face (photolib's
    answer: person or null), end. A comment line every 15 s keeps the connection alive."""
    from fastapi.responses import StreamingResponse
    q = queue.Queue(maxsize=500)
    with _subs_lock:
        _subs.append(q)

    def gen():
        try:
            yield ": hello\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _subs_lock:
                if q in _subs:
                    _subs.remove(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.get("/probe")
def probe(url: str):
    """One frame from a URL: is it a camera, which size."""
    if not url.startswith(("http://", "https://", "rtsp://", "rtsps://", "gopro://")):
        raise HTTPException(400, "http(s)://, rtsp:// or gopro://")
    r = Reader(url, 1)
    r.start()
    t = time.time()
    while time.time() - t < 12 and not r.frame and not (r.error and time.time() - t > 2):
        time.sleep(0.2)
    r.stop()
    if not r.frame:
        return {"ok": False, "error": r.error or "нет кадра за 12 с"}
    img = decode(r.frame[1], r.frame[2])
    if img is None:
        return {"ok": False, "error": "кадр не читается"}
    return {"ok": True, "size": [img.shape[1], img.shape[0]],
            "kind": "gopro" if url.startswith("gopro") else "rtsp" if url.startswith("rtsp") else ("mjpeg" if len(r.got) > 1 else "jpeg")}
