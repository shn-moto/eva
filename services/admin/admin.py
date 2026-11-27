"""Eva's admin page on the hub: the memory's phrases with their sound, the
service logs, search, keep / delete.

    GET  /                       the page (index.html)
    GET  /api/days               days with phrases (count, kept)
    GET  /api/day?d=&heard=1     the day's phrases; heard=1 adds what was heard but is not in the memory
    GET  /api/search?q=          phrases over all days (text)
    GET  /api/logs?...           service logs (eva_log): day / around a time, units, text, before_id
    GET  /api/units?d=           log units of a day with counts
    POST /api/keep   {id, keep}  keep the sound for good (copied out of the STT log, which drops old days)
    POST /api/delete {id}|{audio} a phrase with its sound (and out of the memory search), or a heard-only clip
    POST /api/correct {id|audio, text, speaker}  the right text / speaker: the memory gets it, what was
                                 heard stays (text_heard, speaker_heard) and goes into the clip's json
    GET  /api/speakers           who the phrases are written to (for the speaker field)
    GET  /audio/<day>/<name>.wav the sound
    POST /api/backfill           link phrases written before the sound was stored to their STT-log clips
    GET/POST /api/eyes/cameras   the cameras (EYES_DIR/eyes.json); /api/eyes/live/* the eyes service;
                                 /api/eyes/days, /api/eyes/events?d= the eyes' journal (eye_events, eye_faces)

Home network only, like photolib: a request from the Cloudflare tunnel or a
public address is refused - this is the family's voices.

    ASSISTANT_PG_DSN   assistant_memory (utterances, segments, eva_log)
    STT_LOG_DIR        the STT log (<day>/<time>.wav + .json), dropped after STT_LOG_DAYS
    KEEP_DIR           sound kept for good (same layout)
"""
import datetime
import difflib
import ipaddress
import json
import os
import re
import shutil

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

DSN = os.environ["ASSISTANT_PG_DSN"]
LOG_DIR = os.environ.get("STT_LOG_DIR", "/log")
KEEP_DIR = os.environ.get("KEEP_DIR", "/keep")
# the hub's voice base (services/stt/voices.py): a corrected speaker teaches it
VOICES_URL = os.environ.get("VOICES_URL", "http://host.docker.internal:8901/voices")
# a speaker outside the family (services/stt/voices.py STRANGER): «Неизвестный1», «Гость 2»
STRANGER = re.compile(r"^\s*(неизвестн\w*|незнаком\w*|гость|чужой)[\s_-]*\d*\s*$", re.I)
HERE = os.path.dirname(os.path.abspath(__file__))
NAME = re.compile(r"^\d{4}-\d{2}-\d{2}/\d{6}-\d{3}$")
app = FastAPI()


@app.middleware("http")
async def home_only(request: Request, call_next):
    ip = request.client.host if request.client else ""
    try:
        private = ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback
    except ValueError:
        private = False
    if not private or request.headers.get("CF-Ray"):
        return JSONResponse({"detail": "home network only"}, status_code=403)
    return await call_next(request)


def db():
    return psycopg.connect(DSN, autocommit=True, connect_timeout=5)


def _local(ts):
    return ts.astimezone().strftime("%H:%M:%S") if ts else None


def _clip(audio):
    """'<day>/<name>#t=..' -> ('<day>/<name>', '#t=..')"""
    if not audio:
        return None, ""
    base, _, frag = audio.partition("#")
    return (base, "#" + frag if frag else "") if NAME.match(base) else (None, "")


def _path(base, ext=".wav"):
    for root in (KEEP_DIR, LOG_DIR):
        p = os.path.join(root, base + ext)
        if os.path.exists(p):
            return p
    return None


def _sound(base, info=None):
    """What the sound was (services/voice-events, written into the clip's json by the hub):
    the speech emotion with its probabilities, and the kinds of sound that stood out - shown
    in the log so it can be told by ear whether they are any good (user, 2026-11-26)."""
    if info is None:
        p = base and _path(base, ".json")
        if not p:
            return None
        try:
            info = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            return None
    s = info.get("sound") or {}
    emo = s.get("emotion") or {}
    kinds = {k: v for k, v in (s.get("kinds") or {}).items() if v >= 0.3 and k not in ("music", "tv")}
    if not emo and not kinds:
        return None
    return {"emo": emo.get("label"), "score": emo.get("score"), "probs": emo.get("probs"), "kinds": kinds}


def _row(r):
    (uid, ts, role, speaker, text, dialog, woke, stt, audio, keep, text_heard, speaker_heard, corrected_at) = r
    base, frag = _clip(audio)
    # the parts of a split clip were written to the memory at one moment: each
    # shows its own start (the clip's + its offset in it), not one time for all
    off = 0.0
    if frag.startswith("#t="):
        try:
            off = float(frag[3:].split(",")[0])
        except ValueError:
            pass
    if off:
        ts = ts + datetime.timedelta(seconds=off)
    return {"id": uid, "t": _local(ts), "day": ts.astimezone().strftime("%Y-%m-%d"), "role": role,
            "speaker": speaker, "text": text, "dialog": dialog, "woke": woke, "stt": stt,
            "audio": base, "frag": frag, "has_audio": bool(base and _path(base)), "keep": keep,
            "text_heard": text_heard, "speaker_heard": speaker_heard, "corrected": corrected_at is not None,
            "sound": _sound(base) if base and role == "user" else None}


COLS = "id, ts, role, speaker, text, dialog, woke, stt, audio, keep, text_heard, speaker_heard, corrected_at"


@app.get("/")
def page():
    # no-cache: a new version of the page shows at once (the old one stayed after a deploy)
    return HTMLResponse(open(os.path.join(HERE, "index.html"), encoding="utf-8").read(),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/days")
def days():
    with db() as c:
        rows = c.execute("SELECT (ts AT TIME ZONE current_setting('TimeZone'))::date d, count(*),"
                         " count(*) FILTER (WHERE keep), count(*) FILTER (WHERE audio IS NOT NULL)"
                         " FROM utterances GROUP BY 1 ORDER BY 1 DESC LIMIT 400").fetchall()
    out = {str(d): {"day": str(d), "n": n, "kept": k, "audio": a} for d, n, k, a in rows}
    if os.path.isdir(LOG_DIR):
        for d in os.listdir(LOG_DIR):
            # only the day folders: the ear keeps its own files there too (ear-null.npz, ear-box/)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and os.path.isdir(os.path.join(LOG_DIR, d)):
                out.setdefault(d, {"day": d, "n": 0, "kept": 0, "audio": 0})
    return sorted(out.values(), key=lambda x: x["day"], reverse=True)


def _who(info):
    """The voice(s) of a heard clip as kept by the hub ("who"): a guest group
    «гость-N» is shown as «Неизвестный N» - the same stranger, the same name."""
    names = []
    for w in info.get("who") or []:
        lab = w.get("label")
        if not lab or lab == "unknown":
            continue
        if lab.startswith("гость-"):
            lab = "Неизвестный " + lab.split("-", 1)[1]
        if lab not in names:
            names.append(lab)
    return " / ".join(names) or None


def _heard(day, linked):
    """STT-log clips of the day that no phrase in the memory points to (guests,
    the TV, children's chatter, phrases not to her)."""
    out = []
    for root in (LOG_DIR, KEEP_DIR):
        d = os.path.join(root, day)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json") or "-raw" in fn:
                continue
            base = f"{day}/{fn[:-5]}"
            if base in linked or any(x["audio"] == base for x in out):
                continue
            try:
                info = json.load(open(os.path.join(d, fn), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not (info.get("text") or "").strip():
                continue                  # noise the STT made nothing of
            if info.get("joined_into"):
                continue                  # a piece glued with its rest: the whole is shown
            if info.get("echo"):
                info["who"] = [{"label": "Ева (эхо)"}]       # her own voice, caught in the log
            elif info.get("muted"):
                # from a muted place (services/stt/hear.py, the «Места» page)
                m = info["muted"]
                info["who"] = [{"label": (m.get("place") or "вне мест") + " (подавлено)"}]
            elif info.get("noise") or (info.get("sound") or {}).get("verdict") == "noise":
                top = ((info.get("sound") or {}).get("top") or [["", 0]])[0][0]
                info["who"] = [{"label": "шум" + (f" ({top})" if top else "")}]
            elif info.get("window") and not info.get("who"):
                # a window over music (not her voice: the ear's playback share is low) is not "her speech"
                info["who"] = [{"label": "во время речи Евы" if (info.get("playback") or 0) >= 0.5 else "во время музыки"}]
            hh = fn[:6]
            fix = info.get("correct") or {}
            if isinstance(fix, list):
                fix = next((f for f in fix if not f.get("span")), fix[-1] if fix else {})
            out.append({"id": None, "t": f"{hh[:2]}:{hh[2:4]}:{hh[4:6]}", "day": day, "role": "heard",
                        "speaker": fix.get("speaker") or _who(info), "text": fix.get("text") or info.get("text") or "",
                        "text_heard": info.get("text") if fix else None, "speaker_heard": None,
                        "corrected": bool(fix), "audio": base, "frag": "",
                        "has_audio": os.path.exists(os.path.join(d, fn[:-5] + ".wav")),
                        "keep": root == KEEP_DIR, "dur": info.get("duration"), "sound": _sound(base, info)})
    return out


@app.get("/api/day")
def day(d: str, heard: int = 0):
    datetime.date.fromisoformat(d)
    with db() as c:
        rows = [_row(r) for r in c.execute(
            f"SELECT {COLS} FROM utterances WHERE (ts AT TIME ZONE current_setting('TimeZone'))::date = %s"
            " ORDER BY ts, id", (d,)).fetchall()]
    if heard:
        rows += _heard(d, {r["audio"] for r in rows if r["audio"]})
        rows.sort(key=lambda r: (r["t"], r["id"] or 0))
    return rows


@app.get("/api/search")
def search(q: str, limit: int = 300):
    q = q.strip()
    if len(q) < 2:
        return []
    with db() as c:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        rows = c.execute(f"SELECT {COLS} FROM utterances WHERE text ILIKE %s OR text_heard ILIKE %s"
                         " ORDER BY ts DESC LIMIT %s", (like, like, min(limit, 1000))).fetchall()
    return [_row(r) for r in rows]


NOISY = ("eva-ui.service", "spike-cog.service", "eva-ui-http.service", "init.scope", "kernel", "systemd",
         "dropbear.service")


@app.get("/api/units")
def units(d: str):
    datetime.date.fromisoformat(d)
    with db() as c:
        rows = c.execute("SELECT unit, count(*) FROM eva_log WHERE (ts AT TIME ZONE current_setting('TimeZone'))::date = %s"
                         " GROUP BY 1 ORDER BY 2 DESC", (d,)).fetchall()
    return [{"unit": u, "n": n, "noisy": u in NOISY} for u, n in rows]


@app.get("/api/logs")
def logs(d: str = "", around: str = "", units: str = "", q: str = "", before_id: int = 0, minutes: int = 3,
         limit: int = 400):
    """around=HH:MM:SS (with d): the logs ± minutes around a phrase; else the day, newest first."""
    where, args = [], []
    if d:
        datetime.date.fromisoformat(d)
        if around:
            t = datetime.datetime.fromisoformat(f"{d}T{around}").astimezone()
            where.append("ts BETWEEN %s AND %s")
            args += [t - datetime.timedelta(minutes=minutes), t + datetime.timedelta(minutes=minutes)]
        else:
            where.append("(ts AT TIME ZONE current_setting('TimeZone'))::date = %s")
            args.append(d)
    if units:
        where.append("unit = ANY(%s)")
        args.append(units.split(","))
    if q.strip():
        where.append("msg ILIKE %s")
        args.append("%" + q.strip() + "%")
    if before_id:
        where.append("id < %s")
        args.append(before_id)
    sql = ("SELECT id, ts, unit, prio, msg FROM eva_log" + (" WHERE " + " AND ".join(where) if where else "")
           + (" ORDER BY ts, id" if around else " ORDER BY id DESC") + " LIMIT %s")
    with db() as c:
        rows = c.execute(sql, args + [min(limit, 2000)]).fetchall()
    return [{"id": i, "t": _local(ts), "unit": u.replace(".service", ""), "prio": p, "msg": m}
            for i, ts, u, p, m in rows]


@app.get("/audio/{day}/{name}")
def audio(day: str, name: str):
    base = f"{day}/{name[:-4] if name.endswith('.wav') else name}"
    if not NAME.match(base):
        raise HTTPException(404)
    p = _path(base)
    if not p:
        raise HTTPException(404, "the sound is gone (older than the STT log keeps)")
    return FileResponse(p, media_type="audio/wav")


def _copy_out(base):
    for ext in (".wav", ".json"):
        src = os.path.join(LOG_DIR, base + ext)
        dst = os.path.join(KEEP_DIR, base + ext)
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


def _remove(base):
    for root in (KEEP_DIR, LOG_DIR):
        for ext in (".wav", ".json"):
            p = os.path.join(root, base + ext)
            if os.path.exists(p):
                os.remove(p)


@app.post("/api/keep")
async def keep(request: Request):
    b = await request.json()
    want = bool(b.get("keep", True))
    if b.get("id"):
        with db() as c:
            r = c.execute("UPDATE utterances SET keep = %s WHERE id = %s RETURNING audio", (want, int(b["id"]))).fetchone()
        if not r:
            raise HTTPException(404)
        base, _ = _clip(r[0])
    else:
        base, _ = _clip(b.get("audio"))
        if not base:
            raise HTTPException(400)
    if base and want:
        _copy_out(base)
    elif base and not want:
        # back to the STT log's lifetime: the kept copy goes if the log still has it
        if os.path.exists(os.path.join(LOG_DIR, base + ".wav")):
            for ext in (".wav", ".json"):
                p = os.path.join(KEEP_DIR, base + ext)
                if os.path.exists(p):
                    os.remove(p)
    return {"ok": True, "keep": want}


def _forget(c, uid, drop=True):
    """The segments built from this phrase (the memory search) are rebuilt from
    their phrases - without it (deleted) or with its new text (corrected) - with
    no correction and no embedding: the indexer redoes both (NULL = to do)."""
    for sid, src in c.execute("SELECT id, src_ids FROM segments WHERE %s = ANY(src_ids)", (uid,)).fetchall():
        rest = [i for i in src if i != uid or not drop]
        if not rest:
            c.execute("DELETE FROM segments WHERE id = %s", (sid,))
            continue
        texts = [t for (t,) in c.execute("SELECT text FROM utterances WHERE id = ANY(%s) ORDER BY id", (rest,))]
        c.execute("UPDATE segments SET src_ids = %s, text = %s, corrected = NULL, embedding = NULL WHERE id = %s",
                  (rest, " ".join(t.strip() for t in texts if t), sid))


@app.post("/api/delete")
async def delete(request: Request):
    b = await request.json()
    if b.get("id"):
        uid = int(b["id"])
        with db() as c:
            with c.transaction():
                r = c.execute("SELECT audio FROM utterances WHERE id = %s", (uid,)).fetchone()
                if not r:
                    raise HTTPException(404)
                _forget(c, uid)
                c.execute("DELETE FROM utterances WHERE id = %s", (uid,))
                base, _ = _clip(r[0])
                # the same clip may hold another phrase (two voices split): keep it then
                other = base and c.execute("SELECT 1 FROM utterances WHERE audio LIKE %s LIMIT 1",
                                           (base + "%",)).fetchone()
        if base and not other:
            _remove(base)
        return {"ok": True}
    base, _ = _clip(b.get("audio"))
    if not base:
        raise HTTPException(400)
    _remove(base)
    return {"ok": True}


@app.post("/api/backfill")
def backfill():
    """Phrases written before their sound was stored: the STT-log clip of the
    same text a few seconds before the phrase reached the memory."""
    linked = 0
    with db() as c:
        rows = c.execute("SELECT id, ts, text FROM utterances WHERE role = 'user' AND audio IS NULL"
                         " AND stt = 'gigaam' ORDER BY ts").fetchall()
        used = {a.split("#")[0] for (a,) in c.execute("SELECT audio FROM utterances WHERE audio IS NOT NULL")}
        clips = {}
        for uid, ts, text in rows:
            lt = ts.astimezone()
            day = lt.strftime("%Y-%m-%d")
            if day not in clips:
                clips[day] = [x for x in _heard(day, used)]
            best, score = None, 0.0
            for x in clips[day]:
                hh = x["t"]
                ct = datetime.datetime.fromisoformat(f"{day}T{hh}").astimezone()
                dt = (lt - ct).total_seconds()
                if not -2 <= dt <= 25:
                    continue
                s = difflib.SequenceMatcher(None, (text or "").lower(), (x["text"] or "").lower()).ratio()
                if s > score:
                    best, score = x, s
            if best and score >= 0.75:
                c.execute("UPDATE utterances SET audio = %s WHERE id = %s", (best["audio"], uid))
                used.add(best["audio"])
                clips[day].remove(best)
                linked += 1
    return {"checked": len(rows), "linked": linked}


@app.get("/api/speakers")
def speakers():
    with db() as c:
        return [s for (s,) in c.execute("SELECT speaker FROM utterances WHERE role = 'user' AND speaker IS NOT NULL"
                                        " AND speaker <> '' GROUP BY 1 ORDER BY count(*) DESC")]


def _mark_clip(base, text, speaker, heard_text, frag=""):
    """The clip's json gets the right answer next to what the STT heard: a
    labelled sample for fine-tuning and testing the recognition. The clip is
    kept for good (copied out of the log, which drops old days)."""
    if not base:
        return
    _copy_out(base)
    for root in (KEEP_DIR, LOG_DIR):
        p = os.path.join(root, base + ".json")
        if not os.path.exists(p):
            continue
        try:
            info = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        # a list: one clip may hold two phrases (split between two voices), each
        # with its own span of the clip - a sample is the span's sound + its text
        span = [float(x) for x in frag[3:].split(",")] if frag.startswith("#t=") else None
        fixes = info.get("correct")
        fixes = [fixes] if isinstance(fixes, dict) else list(fixes or [])
        fixes = [f for f in fixes if f.get("span") != span]
        fixes.append({"text": text, "speaker": speaker, "heard": heard_text, "span": span,
                      "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
        info["correct"] = fixes
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)


def _teach_voice(uid, speaker, heard, audio):
    """The phrase's print into that person's voice, the way «запомни мой голос»
    does - a person said whose voice it is. -> what the base did, or the error."""
    import urllib.request
    req = urllib.request.Request(VOICES_URL + "/correct", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"speaker": speaker, "heard": heard, "audio": audio,
                                                  "utterance_id": uid}).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:      # the hub away: the correction stays, the voice is not taught
        return {"error": str(e)}


def _hub(path, body=None, timeout=10):
    import urllib.request
    req = urllib.request.Request(VOICES_URL + path, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---- who said what, read again after the voices learnt (services/stt/rescan.py) ----
@app.post("/api/rescan")
async def rescan_start(request: Request):
    b = await request.json()
    try:
        return _hub("/rescan", {"days": int(b.get("days", 30))})
    except Exception as e:
        raise HTTPException(502, f"hub: {e}")


@app.get("/api/rescan")
def rescan_status():
    try:
        return _hub("/rescan/status")
    except Exception as e:
        raise HTTPException(502, f"hub: {e}")


@app.post("/api/voice-skip")
async def voice_skip(request: Request):
    """A clip (or a part) not to be used for voices: several voices, noise, the TV.
    The rescan never proposes it again."""
    b = await request.json()
    audio = (b.get("audio") or "").strip()
    if not audio or not _clip(audio)[0]:
        raise HTTPException(400)
    with db() as c:
        if b.get("skip", True):
            c.execute("INSERT INTO voice_skip (audio) VALUES (%s) ON CONFLICT DO NOTHING", (audio,))
        else:
            c.execute("DELETE FROM voice_skip WHERE audio = %s", (audio,))
    return {"ok": True}


@app.post("/api/rescan/apply")
async def rescan_apply(request: Request):
    """The proposals a person kept: a phrase she labelled herself gets the voice
    it is now (what she said then stays in speaker_heard; a person's correction
    is never touched); a heard-only clip of a known voice goes into the memory,
    marked as found by the voice, not as a correction."""
    items = (await request.json()).get("items", [])
    done = {"retag": 0, "heard": 0, "skipped": 0}
    with db() as c:
        for it in items:
            if it.get("kind") == "retag" and it.get("id"):
                r = c.execute("UPDATE utterances SET speaker_heard = coalesce(speaker_heard, speaker), speaker = %s"
                              " WHERE id = %s AND corrected_at IS NULL AND speaker IS NOT DISTINCT FROM %s RETURNING id",
                              (it["new"], int(it["id"]), it.get("old"))).fetchone()
                done["retag" if r else "skipped"] += 1
            elif it.get("kind") == "heard" and it.get("audio"):
                base, _ = _clip(it["audio"])
                if not base or c.execute("SELECT 1 FROM utterances WHERE audio = %s OR audio LIKE %s",
                                         (base, base + "#%")).fetchone():
                    done["skipped"] += 1
                    continue
                day, name = base.split("/")
                ts = datetime.datetime.strptime(day + name[:6], "%Y-%m-%d%H%M%S").astimezone()
                c.execute("INSERT INTO utterances (ts, role, text, speaker, dialog, woke, stt, audio, speaker_heard)"
                          " VALUES (%s, 'user', %s, %s, false, false, 'gigaam', %s, 'не узнан')",
                          (ts, it.get("text") or "", it["new"], base))
                done["heard"] += 1
    return done


@app.post("/api/correct")
async def correct(request: Request):
    b = await request.json()
    text = (b.get("text") or "").strip()
    speaker = (b.get("speaker") or "").strip() or None
    if not text:
        raise HTTPException(400, "empty text: delete the phrase instead")
    taught = None
    if b.get("id"):
        uid = int(b["id"])
        if speaker == "-":
            # «-»: not who she said, somebody unknown - the hub gives the voice a
            # stranger's name (its guest group's number, or a new one) and learns it
            with db() as c:
                r0 = c.execute("SELECT speaker, speaker_heard, audio FROM utterances WHERE id = %s", (uid,)).fetchone()
            if not r0 or not r0[2]:
                raise HTTPException(400, "no sound to tell the voice by")
            taught = _teach_voice(uid, "-", r0[1] or r0[0], r0[2])
            speaker = (taught or {}).get("profile") or "Неизвестный"
        with db() as c:
            with c.transaction():
                r = c.execute("SELECT text, speaker, text_heard, speaker_heard, audio FROM utterances WHERE id = %s",
                              (uid,)).fetchone()
                if not r:
                    raise HTTPException(404)
                old_text, old_speaker, heard, heard_speaker, audio = r
                speaker = speaker or old_speaker
                # what the STT heard is kept once: a second correction does not overwrite it
                if heard is None:
                    heard = old_text
                if heard_speaker is None and speaker != old_speaker:
                    heard_speaker = old_speaker
                c.execute("UPDATE utterances SET text = %s, speaker = %s, text_heard = %s, speaker_heard = %s,"
                          " corrected_at = now(), keep = keep OR audio IS NOT NULL WHERE id = %s",
                          (text, speaker, heard, heard_speaker, uid))
                _forget(c, uid, drop=False)
        base, frag = _clip(audio)
        _mark_clip(base, text, speaker, heard, frag)
        out = {"ok": True, "speaker": speaker}
        if taught is not None:
            out["voice"] = taught
        elif speaker and speaker != old_speaker and audio:
            out["voice"] = _teach_voice(uid, speaker, heard_speaker or old_speaker, audio)
        if speaker and STRANGER.match(speaker):
            # somebody outside the family: the voice is learnt under that name (above),
            # the phrase leaves the memory - it stays a heard clip with the mark
            with db() as c:
                with c.transaction():
                    _forget(c, uid)
                    c.execute("DELETE FROM utterances WHERE id = %s", (uid,))
            out["removed"] = True
        return out
    base, _ = _clip(b.get("audio"))
    if not base:
        raise HTTPException(400)
    p = _path(base, ".json")
    heard = None
    if p:
        try:
            heard = json.load(open(p, encoding="utf-8")).get("text")
        except (OSError, ValueError):
            pass
    if speaker == "-":
        taught = _teach_voice(None, "-", None, base)
        speaker = (taught or {}).get("profile") or "Неизвестный"
    _mark_clip(base, text, speaker, heard)
    out = {"ok": True, "speaker": speaker}
    if taught is not None:
        out["voice"] = taught
    elif speaker and STRANGER.match(speaker):
        # a stranger named: the voice learns it (not taken for family again), the memory does not
        out["voice"] = _teach_voice(None, speaker, None, base)
    elif speaker:
        # a heard-only clip (a voice she did not know) with its speaker named by a
        # person is a phrase like any other: into the memory, shown in the log,
        # and its print into that voice (2026-11-24)
        uid = _to_memory(base, text, speaker, heard)
        if uid:
            out["id"] = uid
            out["voice"] = _teach_voice(uid, speaker, None, base)
    return out


def _to_memory(base, text, speaker, heard):
    """A heard-only clip -> a phrase in the memory (once). -> its id, or None."""
    day, name = base.split("/")
    ts = datetime.datetime.strptime(day + name[:6], "%Y-%m-%d%H%M%S").astimezone()
    with db() as c:
        if c.execute("SELECT 1 FROM utterances WHERE audio = %s OR audio LIKE %s",
                     (base, base + "#%")).fetchone():
            return None
        uid = c.execute(
            "INSERT INTO utterances (ts, role, text, speaker, dialog, woke, stt, audio, keep, text_heard, corrected_at)"
            " VALUES (%s, 'user', %s, %s, false, false, 'gigaam', %s, true, %s, now()) RETURNING id",
            (ts, text, speaker, base, heard)).fetchone()[0]
    _copy_out(base)                       # kept: the STT log drops old days
    return uid


# ---- direction: the radar of a phrase, the room of a day -----------------------------------
# The STT keeps the direction with the clip (json "doa": {"az", "blocks": [[t, az,
# strength]...]}, "parts": [{"from", "to", "az", "cut"}]). Angles are the array's;
# where "in front of the Station" is (rotation) and the mirror are the room's,
# set here once from a phrase said in front of it.
SETTINGS = os.path.join(KEEP_DIR, "admin-settings.json")


def _settings():
    try:
        return json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError):
        return {"rotation": 0.0, "mirror": False}


@app.get("/api/settings")
def get_settings():
    return _settings()


@app.post("/api/settings")
async def set_settings(request: Request):
    b = await request.json()
    s = _settings()
    if "rotation" in b:
        s["rotation"] = float(b["rotation"]) % 360
    if "mirror" in b:
        s["mirror"] = bool(b["mirror"])
    # places learned from labelled phrases: from other rooms the sound comes in
    # through doors and walls, so its angle is not the room's on a plan - but it
    # repeats (2026-11-24: the bedroom 36, the closet 78, the toilet 82 degrees)
    if "place" in b:
        pl = b["place"]
        places = [x for x in s.get("places", []) if not (pl.get("remove") and x["name"] == pl["name"])]
        if not pl.get("remove"):
            places.append({"name": str(pl["name"])[:40], "az": float(pl["az"]) % 360,
                           "audio": pl.get("audio"), "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
        s["places"] = places
        if pl.get("remove"):
            (s.get("place_opts") or {}).pop(pl["name"], None)
    if "place_opt" in b:
        # per place: "mode" - the logical one: "mute" (not listened to: the TV) | "only" (listen only
        # to such places) | "" normal; "null" - the sound null by the mics (services/stt/ear_null.py)
        o = b["place_opt"]
        opts = s.setdefault("place_opts", {})
        cur = dict(opts.get(o["name"]) or {})
        if "mode" in o:
            if o.get("mode") in ("mute", "only"):
                cur["mode"] = o["mode"]
            else:
                cur.pop("mode", None)
        if "null" in o:
            if o.get("null"):
                cur["null"] = True
            else:
                cur.pop("null", None)
        if cur:
            opts[str(o["name"])[:40]] = cur
        else:
            opts.pop(o["name"], None)
    if "null_on" in b:
        s["null_on"] = bool(b["null_on"])          # the sound null's master switch
    os.makedirs(KEEP_DIR, exist_ok=True)
    with open(SETTINGS + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(SETTINGS + ".tmp", SETTINGS)
    return s


@app.get("/api/null")
def null_state():
    """The sound null's weights as the hub saved them (stt-log/ear-null.json): for which places,
    when, how much they take from the place and from speech elsewhere (its json beside them)."""
    try:
        return json.load(open(os.path.join(LOG_DIR, "ear-null.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _info(base):
    p = _path(base, ".json")
    if not p:
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return None


@app.get("/api/clip")
def clip(audio: str):
    base, _ = _clip(audio)
    if not base:
        raise HTTPException(400)
    info = _info(base)
    if info is None:
        raise HTTPException(404, "no clip json")
    return {"duration": info.get("duration"), "text": info.get("text"), "doa": info.get("doa"),
            "parts": info.get("parts"), "split": {k: v for k, v in (info.get("split") or {}).items() if k != "embs"} or None}


@app.get("/api/room")
def room(d: str):
    """Every clip of the day with its angle (and its parts'), the speaker from the memory."""
    datetime.date.fromisoformat(d)
    with db() as c:
        who = {}
        for audio, speaker, text in c.execute(
                "SELECT audio, speaker, text FROM utterances WHERE audio IS NOT NULL"
                " AND (ts AT TIME ZONE current_setting('TimeZone'))::date = %s", (d,)):
            base, frag = _clip(audio)
            who[(base, frag)] = (speaker, text)
    out = []
    for root in (LOG_DIR, KEEP_DIR):
        dd = os.path.join(root, d)
        if not os.path.isdir(dd):
            continue
        for fn in sorted(os.listdir(dd)):
            if not fn.endswith(".json") or fn == "admin-settings.json":
                continue
            base = f"{d}/{fn[:-5]}"
            if any(x["audio"] == base for x in out):
                continue
            try:
                info = json.load(open(os.path.join(dd, fn), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            doa = info.get("doa") or {}
            parts = info.get("parts") or []
            hh = fn[:6]
            t = f"{hh[:2]}:{hh[2:4]}:{hh[4:6]}"
            if parts:
                for k, p in enumerate(parts):
                    frag = f"#t={p['from']:g}" + (f",{p['to']:g}" if k < len(parts) - 1 else "") if p["from"] else (
                        f"#t=0,{p['to']:g}")
                    sp, text = who.get((base, frag), who.get((base, ""), (None, p.get("text"))))
                    if p.get("az") is not None:
                        out.append({"audio": base, "t": t, "az": p["az"], "speaker": sp, "text": text or p.get("text"),
                                    "part": k + 1})
            elif doa.get("az") is not None:
                sp, text = who.get((base, ""), (None, info.get("text")))
                out.append({"audio": base, "t": t, "az": doa["az"], "strength": doa.get("strength"),
                            "speaker": sp, "text": text})
    return out


# ---- the brain's calendar and listeners (app/core/calendar.py, app/core/listeners.py) -------
# The brain's state dir is mounted here (BRAIN_DATA). The calendar is read-modify-written
# under the same flock as the brain's scheduler; a new event gets "pending": the brain
# puts it on its first real time (repeats, weekdays) at its next tick.
BRAIN = os.environ.get("BRAIN_DATA", "/brain")
CAL = os.path.join(BRAIN, "calendar.json")
LST = os.path.join(BRAIN, "listeners.json")
PHOTOLIB = os.environ.get("PHOTOLIB_URL", "http://host.docker.internal:8000")
ROUTER = os.environ.get("ROUTER_URL", "http://host.docker.internal:8906/v1/chat/completions")


class _CalLock:
    def __enter__(self):
        import fcntl
        self.f = open(CAL + ".lock", "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)

    def __exit__(self, *a):
        self.f.close()


def _jload(path, empty):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return empty


def _jsave(path, d):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def _birthdays(days=60):
    import urllib.request
    try:
        with urllib.request.urlopen(PHOTOLIB + "/persons/family-tree", timeout=10) as r:
            persons = json.load(r).get("persons", [])
    except Exception:
        return []
    today, out = datetime.date.today(), []
    for p in persons:
        b = str(p.get("birth_date") or "")[:10]
        if not b or p.get("death_date") or p.get("birth_date_approx"):
            continue
        try:
            b = datetime.date.fromisoformat(b)
            nxt = b.replace(year=today.year)
        except ValueError:
            continue
        if nxt < today:
            nxt = nxt.replace(year=today.year + 1)
        if (nxt - today).days <= days:
            out.append({"date": nxt.isoformat(), "name": p.get("name"), "age": nxt.year - b.year})
    return sorted(out, key=lambda x: x["date"])


@app.get("/api/calendar")
def calendar_get():
    d = _jload(CAL, {"events": []})
    return {"events": sorted(d.get("events", []), key=lambda e: e.get("next", "")), "birthdays": _birthdays()}


@app.post("/api/calendar")
async def calendar_post(request: Request):
    b = await request.json()
    with _CalLock():
        d = _jload(CAL, {"events": [], "said": {}})
        ev = d.setdefault("events", [])
        if b.get("remove"):
            d["events"] = [e for e in ev if e["id"] != b["remove"]]
        else:
            e = {k: b[k] for k in ("title", "action", "text", "query", "notify", "who", "repeat") if b.get(k)}
            if not e.get("title") or e.get("action") not in ("remind", "music", "alarm", "task") or not b.get("when"):
                raise HTTPException(400, "title, action, when")
            datetime.datetime.fromisoformat(b["when"].replace(" ", "T"))
            e.update(id=b.get("id") or os.urandom(3).hex(), next=b["when"].replace(" ", "T")[:16], pending=True,
                     by="админка", channel="admin", created=datetime.datetime.now().strftime("%Y-%m-%dT%H:%M"))
            d["events"] = [x for x in ev if x["id"] != e["id"]] + [e]
        _jsave(CAL, d)
    return calendar_get()


MUSIC = os.environ.get("MUSIC_URL", "http://host.docker.internal:8904")


@app.get("/api/music/search")
def music_search(q: str):
    """YouTube Music hits for a listener's song, each checked to play: a listener keeps the
    chosen track's link - by words the search drifted to another song, and the first hit
    of the Leopold song was "not available" (2026-11-27)."""
    import urllib.parse
    import urllib.request
    p = q.strip()
    if p.startswith("/media/") and not p.lower().endswith(AUDIO_EXTS):
        # a folder: the listener plays all its tracks (shuffled) - list them instead of checking one
        files = [f for f in media_list()["files"] if f["path"].startswith(p.rstrip("/") + "/")]
        return {"hits": [{"title": f"папка {p}: {len(files)} треков, играют все вперемешку", "url": p.rstrip("/") + "/",
                          "folder": True, "error": None if files else "в папке нет аудио"}]}
    if re.match(r"https?://\S+$", q.strip()) or q.strip().startswith("/media/"):   # a link or a local file: check it
        return {"hits": [_music_check({"title": q.strip(), "url": q.strip()})]}
    with urllib.request.urlopen(f"{MUSIC}/search?q={urllib.parse.quote(q)}&kind=track&limit=6", timeout=150) as r:
        hits = json.loads(r.read())
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(6) as ex:
        return {"hits": list(ex.map(_music_check, hits))}


def _music_check(t):
    """Does it play: the music service's /info (yt-dlp) - its error, or the track's facts."""
    import urllib.parse
    import urllib.request
    try:
        with urllib.request.urlopen(f"{MUSIC}/info?url={urllib.parse.quote(t['url'], safe='')}", timeout=90) as r:
            i = json.loads(r.read())
        return dict(t, **{k: v for k, v in i.items() if v and k != "url"}, error=i.get("error"))
    except Exception as e:
        return dict(t, error=str(e)[:200])


MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
MEDIA_SETTINGS = os.environ.get("MEDIA_SETTINGS", "/settings/media.json")


# ---- the paid API switch (app/core/cost.py paid_on): off - the brain makes no Claude call ----
PAID = os.path.join(os.environ.get("BRAIN_DATA", "/brain"), "paid.json")


@app.get("/api/paid")
def paid_get():
    s = _jload(PAID, {})
    spend = _jload(os.path.join(os.environ.get("BRAIN_DATA", "/brain"), "spend.json"), {})
    today = spend.get(datetime.date.today().isoformat(), {})
    return {"on": s.get("on", True) is not False, "today": round(today.get("usd", 0.0), 2),
            "by_channel": today.get("by_channel", {})}


@app.post("/api/paid")
async def paid_post(request: Request):
    on = bool((await request.json()).get("on"))
    with open(PAID + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"on": on, "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}, f)
    os.replace(PAID + ".tmp", PAID)
    return paid_get()


# ---- «Ресурсы»: the media library's roots and the DLNA renderer (services/music/video.py reads them) ----
@app.get("/api/resources")
def resources_get():
    s = _jload(MEDIA_SETTINGS, {})
    return {"video_roots": s.get("video_roots", []), "music_roots": s.get("music_roots", ["/media"]),
            "renderer": s.get("renderer")}


@app.post("/api/resources")
async def resources_post(request: Request):
    b = await request.json()
    s = _jload(MEDIA_SETTINGS, {})
    for k in ("video_roots", "music_roots"):
        if k in b:
            roots = [str(x).strip().rstrip("/") for x in b[k] if str(x).strip()]
            bad = [r for r in roots if not r.startswith(("/home/", "/mnt/", "/media"))]
            if bad:
                raise HTTPException(400, f"папки только под /home, /mnt или /media: {', '.join(bad)}")
            s[k] = roots
    if "renderer" in b:
        s["renderer"] = b["renderer"] or None
    os.makedirs(os.path.dirname(MEDIA_SETTINGS), exist_ok=True)
    with open(MEDIA_SETTINGS + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(MEDIA_SETTINGS + ".tmp", MEDIA_SETTINGS)
    return resources_get()


@app.get("/api/mm/{path:path}")
def media_proxy(path: str, request: Request):
    """The music service's media library and DLNA control as they are (lists, posters, play, state)."""
    import urllib.error
    import urllib.request
    from fastapi.responses import Response
    if not path.startswith(("library/", "dlna/")):
        raise HTTPException(404, "not found")
    url = f"{MUSIC}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return Response(r.read(), media_type=r.headers.get("Content-Type"),
                            headers={"Cache-Control": r.headers.get("Cache-Control") or "no-store"})
    except urllib.error.HTTPError as e:
        return Response(e.read(), status_code=e.code, media_type=e.headers.get("Content-Type"))
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")


@app.get("/api/music/prefetch")
def music_prefetch(url: str):
    """A chosen YouTube track into the music service's cache now: the listener's song then starts at
    once, not after YouTube's ~9 s (2026-11-27)."""
    import urllib.parse
    import urllib.request
    with urllib.request.urlopen(f"{MUSIC}/prefetch?url={urllib.parse.quote(url, safe='')}", timeout=10) as r:
        return json.loads(r.read())


@app.get("/api/media")
def media_list():
    """The local audio files (~/eva-data/media on the hub), as a listener names them: /media/<name>."""
    out = []
    if os.path.isdir(MEDIA_DIR):
        for root, _, files in os.walk(MEDIA_DIR):
            for fn in sorted(files):
                if fn.lower().endswith(AUDIO_EXTS):
                    p = os.path.join(root, fn)
                    out.append({"path": "/media/" + os.path.relpath(p, MEDIA_DIR).replace(os.sep, "/"),
                                "bytes": os.path.getsize(p)})
    return {"files": out}


@app.post("/api/media")
async def media_upload(request: Request, name: str):
    """An audio file from the page (the raw body) -> /media/<name>."""
    base = os.path.basename(name).strip()
    if not re.fullmatch(r"[\w .()\-]+\.(mp3|m4a|aac|ogg|opus|wav|flac)", base, re.I | re.U):
        raise HTTPException(400, "имя: буквы, цифры, пробел, .-() и расширение аудио")
    body = await request.body()
    if not 1000 < len(body) < 200_000_000:
        raise HTTPException(400, "размер")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    with open(os.path.join(MEDIA_DIR, base), "wb") as fo:
        fo.write(body)
    return {"path": "/media/" + base}


@app.get("/api/music/stream")
def music_stream(url: str):
    """The track as the Station would hear it (the music service's mp3), to listen before choosing."""
    import urllib.parse
    import urllib.request
    from fastapi.responses import StreamingResponse
    r = urllib.request.urlopen(f"{MUSIC}/stream?url={urllib.parse.quote(url, safe='')}", timeout=60)

    def body():
        try:
            while chunk := r.read(16384):
                yield chunk
        finally:
            r.close()
    return StreamingResponse(body(), media_type="audio/mpeg")


@app.get("/api/listeners")
def listeners_get():
    items = _jload(LST, {"listeners": []}).get("listeners", [])
    return {"listeners": items, "log": _listener_events()}


def _listener_events(per=12, kind="listener"):
    """The brain's listener lines grouped into firings: "fired on ..." opens one, its
    "ok: ..." / "error: ..." line (the brain logs one per firing since 2026-11-27) is the
    status, anything else in between is detail. A firing without a status yet is
    "pending"; a line with no firing before it (the local model said no) stands alone."""
    by = {}
    try:
        with db() as c:
            rows = c.execute("SELECT ts, msg FROM eva_log WHERE msg LIKE %s "
                             "AND ts > now() - interval '14 days' ORDER BY ts DESC LIMIT 600", (kind + " %",)).fetchall()
    except Exception:
        return {}
    for ts, msg in reversed(rows):
        m = re.match(kind + r" (\S+): (.*)", msg, re.S)
        if not m:
            continue
        lid, text = m.group(1), m.group(2)
        line = {"t": ts.astimezone().strftime("%d.%m %H:%M:%S"), "msg": text[:500]}
        evs = by.setdefault(lid, [])
        st = re.match(r"(ok|error): (.*)", text, re.S)
        if text.startswith("fired on "):
            evs.append(dict(line, msg=text[9:][:300], status="pending", details=[]))
        elif st and evs and evs[-1]["status"] == "pending":
            evs[-1]["status"], evs[-1]["result"] = st.group(1), st.group(2)[:500]
            evs[-1]["details"].append(line)
        elif evs and evs[-1]["status"] == "pending":
            evs[-1]["details"].append(line)
        else:
            evs.append(dict(line, status="error" if st and st.group(1) == "error" else "skip", details=[line]))
    return {k: list(reversed(v))[:per] for k, v in by.items()}


@app.post("/api/listeners")
async def listeners_post(request: Request):
    items = (await request.json()).get("listeners")
    if not isinstance(items, list):
        raise HTTPException(400, "listeners: []")
    seen = set()
    for x in items:
        x["id"] = re.sub(r"[^\w-]", "_", str(x.get("id") or x.get("name") or os.urandom(3).hex()))[:40]
        if x["id"] in seen:
            raise HTTPException(400, f"id {x['id']} twice")
        seen.add(x["id"])
        if x.get("words"):
            try:
                re.compile(x["words"], re.I)
            except re.error as e:
                raise HTTPException(400, f"{x['id']}: words: {e}")
        elif (x.get("enabled") and not x.get("emotion") and not (x.get("prompt") or "").strip()
              and x.get("action", "log") != "log"):
            # empty words = every phrase: a music listener meant for «давайте жить дружно» fired on
            # a laugh 10 min later, and again after each pause (2026-11-27)
            raise HTTPException(400, f"«{x.get('name') or x['id']}»: пустые «Слова» — сработает на КАЖДУЮ фразу. "
                                     "Задай слова (или вопрос модели), либо действие «только в лог»")
    _jsave(LST, {"listeners": items})
    return listeners_get()


@app.get("/api/family")
def family_list():
    """The family members Eva can write to in Telegram (photolib users with a Telegram):
    a choice for «для кого» / «кому написать», not a name typed by hand."""
    import urllib.request
    try:
        with urllib.request.urlopen(PHOTOLIB + "/admin/users", timeout=10) as r:
            users = json.load(r)
        users = users.get("users", []) if isinstance(users, dict) else users
    except Exception:
        return []
    return sorted({u["person_name"] for u in users if u.get("telegram_id") and u.get("person_name")})


@app.get("/api/voices")
def voices_list():
    """The family's voices with samples in the base (services/stt/voices.py) - for "who speaks"."""
    import urllib.request
    try:
        with urllib.request.urlopen(VOICES_URL + "/export", timeout=10) as r:
            return sorted(json.load(r).get("profiles", {}))
    except Exception:
        return []


@app.post("/api/listeners/test")
async def listeners_test(request: Request):
    """A listener over a sample phrase: the words, then the local model's yes/no."""
    import urllib.request
    b = await request.json()
    x, text = b.get("listener") or {}, (b.get("text") or "").strip()
    out = {"words": None, "model": None}
    if x.get("words"):
        try:
            m = re.search(x["words"], text, re.I)
        except re.error as e:
            raise HTTPException(400, f"words: {e}")
        out["words"] = m.group(0) if m else False
    if x.get("prompt") and out["words"] is not False:
        body = {"messages": [
            {"role": "system", "content": "Ты проверяешь фразы, услышанные дома голосовым помощником. "
                                          "Фраза распознана из речи и может содержать ошибки. Ответь только да или нет."},
            {"role": "user", "content": f"Фраза: «{text}»\n\nВопрос: {x['prompt']}"}],
            "temperature": 0, "max_tokens": 4}
        try:
            req = urllib.request.Request(ROUTER, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                out["model"] = json.loads(r.read())["choices"][0]["message"]["content"].strip()
        except Exception as e:
            out["model"] = f"ошибка: {e}"
    return out


# ---- the ear's black box (services/stt/ear_stream.py: Recorder, dump) -------------------
# Recordings of everything the ear gets and makes (2026-11-27: «стоп» over a song never reached
# the ear's cut, and the 60 s box held the mono mix only): started and stopped here, each of the
# 9 tracks listened to here, deleted by a period here.
EAR_URL = os.environ.get("EAR_URL", "http://host.docker.internal:8901")
REC_DIR = os.path.join(LOG_DIR, "ear-rec")
BOX_DIR = os.path.join(LOG_DIR, "ear-box")
COLLECT_FLAG = os.path.join(BOX_DIR, "COLLECT")
TRACK_RU = {"mic1": "мик 1", "mic2": "мик 2", "mic3": "мик 3", "mic4": "мик 4", "mic5": "мик 5", "mic6": "мик 6",
            "mic7": "мик 7", "mix": "смесь микрофонов", "ref": "динамик (опорный)", "aec": "после AEC3",
            "lin": "линейное вычитание"}


def _ear(path, body=None):
    import urllib.request
    req = urllib.request.Request(EAR_URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _phrases_in(t0, t1):
    """What the ear cut and recognised between t0 and t1 (epoch): the STT log's clips, both
    readings (AEC3 output, raw mics) - the black box page lays them over the recording."""
    out = []
    day = datetime.date.fromtimestamp(t0)
    while day <= datetime.date.fromtimestamp(t1):
        d = os.path.join(LOG_DIR, day.isoformat())
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                m = re.fullmatch(r"(\d{6})-(\d{3})\.json", f)
                if not m:
                    continue
                t = datetime.datetime.combine(day, datetime.datetime.strptime(m.group(1), "%H%M%S").time()).timestamp() \
                    + int(m.group(2)) / 1000
                if not t0 - 5 <= t <= t1 + 5:
                    continue
                try:
                    info = json.load(open(os.path.join(d, f), encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                dur = float(info.get("duration") or 0)
                # the clip's own start (hear.py t0); older clips: the name is when STT finished -
                # minus the STT time and the end silence the cut waited (END_SILENCE 0.8 - TAIL_KEEP 0.3)
                t0 = info.get("t0") or t - (info.get("ms") or 0) / 1000 - 0.5 - dur
                if info.get("t0") and not info.get("t1"):
                    t0 += 0.64             # 27.11 14:46-15:00 t0 was the clip's start: its pre-roll (20 chunks) off
                if info.get("t1"):
                    dur = max(0.1, info["t1"] - t0)
                out.append({"t": round(t0, 2), "dur": dur, "src": info.get("src"), "text": info.get("text") or "",
                            "raw_text": info.get("raw_text"), "audio": f"{day.isoformat()}/{f[:-5]}"})
        day += datetime.timedelta(days=1)
    return out


def _box_items():
    import wave
    items = []
    if os.path.isdir(REC_DIR):
        for s in sorted(os.listdir(REC_DIR), reverse=True):
            d = os.path.join(REC_DIR, s)
            if not (re.fullmatch(r"\d{8}-\d{6}", s) and os.path.isdir(d)):
                continue
            try:
                meta = json.load(open(os.path.join(d, "rec.json")))
            except (OSError, ValueError):
                meta = {}
            start = meta.get("start") or datetime.datetime.strptime(s, "%Y%m%d-%H%M%S").timestamp()
            parts = []
            for f in sorted(os.listdir(d)):
                if re.fullmatch(r"\d{6}\.wav", f):
                    p = os.path.join(d, f)
                    try:
                        with wave.open(p) as w:
                            sec = w.getnframes() / w.getframerate()
                    except Exception:            # the minute being written now
                        sec = 0
                    parts.append({"file": f"ear-rec/{s}/{f}", "t": f[:2] + ":" + f[2:4] + ":" + f[4:6],
                                  "seconds": round(sec, 1), "bytes": os.path.getsize(p)})
            items.append({"kind": "rec", "id": s, "start": start,
                          "seconds": meta.get("seconds") or sum(p["seconds"] for p in parts),
                          "on": meta.get("on"), "stopped": meta.get("stopped"), "flags": meta.get("flags", []),
                          "parts": parts, "phrases": _phrases_in(start, start + (meta.get("seconds") or 3600))})
    if os.path.isdir(BOX_DIR):
        for f in sorted(os.listdir(BOX_DIR), reverse=True):
            m = re.fullmatch(r"(\d{8}-\d{6})-([\w-]+)\.npz", f)
            if m:
                t = datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
                items.append({"kind": "box", "id": f, "start": t - 60, "end": t, "reason": m.group(2),
                              "file": f"ear-box/{f}", "bytes": os.path.getsize(os.path.join(BOX_DIR, f)),
                              "phrases": _phrases_in(t - 60, t)})
    return sorted(items, key=lambda i: i["start"], reverse=True)


@app.get("/api/box")
def box_get():
    try:
        rec = _ear("/hear/rec")
    except Exception as e:
        rec = {"on": None, "error": str(e)[:200]}
    return {"rec": rec, "collect": os.path.exists(COLLECT_FLAG), "items": _box_items(), "tracks": TRACK_RU,
            "free": shutil.disk_usage(LOG_DIR).free}


@app.post("/api/box/rec")
async def box_rec(request: Request):
    return _ear("/hear/rec", {"on": bool((await request.json()).get("on"))})


@app.post("/api/box/collect")
async def box_collect(request: Request):
    """The ear's own boxes over her music / voice (ear_stream COLLECT_FLAG), on or off."""
    if (await request.json()).get("on"):
        os.makedirs(BOX_DIR, exist_ok=True)
        open(COLLECT_FLAG, "a").close()
    elif os.path.exists(COLLECT_FLAG):
        os.remove(COLLECT_FLAG)
    return {"collect": os.path.exists(COLLECT_FLAG)}


def _tracks_of(rel):
    """file -> {track: float32 array}, sample rate. A recording minute: 9 channels; a box: its arrays."""
    import numpy as np
    import wave
    if not re.fullmatch(r"ear-rec/\d{8}-\d{6}/\d{6}\.wav|ear-box/[\w-]+\.npz", rel):
        raise HTTPException(400, "file")
    p = os.path.join(LOG_DIR, rel)
    if not os.path.exists(p):
        raise HTTPException(404, "file")
    if rel.endswith(".wav"):
        with wave.open(p) as w:
            n, sr = w.getnchannels(), w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, n).astype(np.float32) / 32768
        names = ["mic1", "mic2", "mic3", "mic4", "mic5", "mic6", "mic7", "ref", "aec", "lin"][:n]
        out = {k: a[:, i] for i, k in enumerate(names)}
    else:
        d = np.load(p)
        sr = int(d["sr"]) if "sr" in d.files else 16000
        out = {"mix": d["y"].astype(np.float32), "ref": d["x"].astype(np.float32), "aec": d["e"].astype(np.float32)}
        if "raw7" in d.files:
            r = d["raw7"].astype(np.float32) / 32768
            out.update({f"mic{i + 1}": r[:, i] for i in range(r.shape[1])})
    if "mix" not in out and "mic1" in out:
        out["mix"] = np.mean([out[f"mic{i}"] for i in range(1, 8) if f"mic{i}" in out], axis=0)
    return out, sr


@app.get("/api/box/track")
def box_track(file: str, track: str):
    """One track as a mono wav, loud enough to hear: the 7 mics share one gain (their levels
    compare), the others are each raised to a peak."""
    import io
    import numpy as np
    import wave
    from fastapi.responses import Response
    tracks, sr = _tracks_of(file)
    if track not in tracks:
        raise HTTPException(404, "track")
    a = tracks[track]
    if track.startswith("mic"):
        peak = max(float(np.abs(v).max()) for k, v in tracks.items() if k.startswith("mic"))
    else:
        peak = float(np.abs(a).max())
    a = a * (0.89 / (peak or 1.0))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(a * 32767, -32767, 32767).astype(np.int16).tobytes())
    return Response(buf.getvalue(), media_type="audio/wav")


@app.post("/api/box/delete")
async def box_delete(request: Request):
    """{"from": epoch, "to": epoch, "dry": bool} - recording minutes and boxes of the period
    (the recording running now is never touched)."""
    b = await request.json()
    t0, t1, dry = float(b.get("from") or 0), float(b.get("to") or 0), bool(b.get("dry"))
    if not t1 > t0:
        raise HTTPException(400, "from < to")
    gone, size = [], 0
    for it in _box_items():
        if it["kind"] == "box" and t0 <= it["end"] <= t1:
            gone.append(it["file"])
        elif it["kind"] == "rec" and not it.get("on"):
            day = datetime.datetime.fromtimestamp(it["start"])
            for p in it["parts"]:
                hh, mm, ss = (int(x) for x in p["t"].split(":"))
                t = day.replace(hour=hh, minute=mm, second=ss).timestamp()
                if t < it["start"] - 1:
                    t += 86400                     # past midnight
                if t0 <= t <= t1:
                    gone.append(p["file"])
    for rel in gone:
        p = os.path.join(LOG_DIR, rel)
        if os.path.exists(p):
            size += os.path.getsize(p)
            if not dry:
                os.remove(p)
    if not dry and os.path.isdir(REC_DIR):
        for s in os.listdir(REC_DIR):              # a recording with no minutes left goes whole
            d = os.path.join(REC_DIR, s)
            if os.path.isdir(d) and not any(f.endswith(".wav") for f in os.listdir(d)):
                shutil.rmtree(d, ignore_errors=True)
    return {"files": len(gone), "bytes": size, "dry": dry}


# ---- «Глаза»: the cameras (services/eyes/eyes.py reads EYES_DIR/eyes.json) and their journal ----
EYES_DIR = os.environ.get("EYES_DIR", "/eyes")
EYES_CONF = os.path.join(EYES_DIR, "eyes.json")
EYES_URL = os.environ.get("EYES_URL", "http://host.docker.internal:8913")
CAM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@app.get("/api/eyes/cameras")
def eyes_get():
    c = _jload(EYES_CONF, {})
    return {"cameras": c.get("cameras", []), "fps": c.get("fps", 5), "quiet_s": c.get("quiet_s", 8),
            "id_threshold": c.get("id_threshold", 0.5), "motion_on": c.get("motion_on", True) is not False,
            "faces_on": c.get("faces_on", True) is not False}


@app.post("/api/eyes/cameras")
async def eyes_post(request: Request):
    """The whole list from the page; the eyes service follows the file within 3 s."""
    b = await request.json()
    c = _jload(EYES_CONF, {})
    for k in ("motion_on", "faces_on"):          # the switches for all the cameras
        if k in b:
            c[k] = bool(b[k])
    if "cameras" not in b:
        b["cameras"] = c.get("cameras", [])
    cams, seen = [], set()
    for x in b.get("cameras", []):
        cid = str(x.get("id", "")).strip().lower()
        url = str(x.get("url", "")).strip()
        if not CAM_ID.match(cid) or cid in seen:
            raise HTTPException(400, f"id камеры: латиница/цифры/-_, без повторов: «{cid}»")
        if not url.startswith(("http://", "https://", "rtsp://", "rtsps://")):
            raise HTTPException(400, f"адрес камеры {cid}: http(s):// или rtsp://")
        seen.add(cid)
        cams.append({"id": cid, "name": str(x.get("name") or cid).strip()[:40], "url": url, "on": bool(x.get("on", True)),
                     "motion": min(0.5, max(0.001, float(x.get("motion") or 0.01))), "faces": bool(x.get("faces", True)),
                     "watch": bool(x.get("watch", True))})
    c["cameras"] = cams
    for k, lo, hi in (("fps", 1, 15), ("quiet_s", 2, 120), ("id_threshold", 0.3, 0.9)):
        if k in b:
            c[k] = min(hi, max(lo, float(b[k])))
    os.makedirs(EYES_DIR, exist_ok=True)
    with open(EYES_CONF + ".tmp", "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=1)
    os.replace(EYES_CONF + ".tmp", EYES_CONF)
    return eyes_get()


@app.get("/api/eyes/live/{path:path}")
def eyes_live(path: str, request: Request):
    """The eyes service as it is: the cameras' state, the newest frame, a probe of a URL."""
    import urllib.error
    import urllib.request
    from fastapi.responses import Response
    if not (path in ("cameras", "probe", "presence") or re.fullmatch(r"frame/[a-z0-9_-]+\.jpg", path)):
        raise HTTPException(404, "not found")
    url = f"{EYES_URL}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return Response(r.read(), media_type=r.headers.get("Content-Type"), headers={"Cache-Control": "no-store"})
    except urllib.error.HTTPError as e:
        return Response(e.read(), status_code=e.code, media_type=e.headers.get("Content-Type"))
    except Exception as e:
        return JSONResponse({"detail": f"глаза не отвечают: {e}"}, status_code=502)


def _eyes_rows(q, args=()):
    try:
        with db() as c:
            return c.execute(q, args).fetchall()
    except psycopg.errors.UndefinedTable:          # the eyes service has not made its tables yet
        return []


@app.get("/api/eyes/days")
def eyes_days():
    rows = _eyes_rows("SELECT (started AT TIME ZONE current_setting('TimeZone'))::date d, count(*), "
                      "count(*) FILTER (WHERE cardinality(people) > 0) FROM eye_events GROUP BY 1 ORDER BY 1 DESC LIMIT 60")
    return {"days": [{"day": d.isoformat(), "events": n, "with_people": p} for d, n, p in rows]}


@app.get("/api/eyes/events")
def eyes_events(d: str, camera: str = ""):
    day = datetime.date.fromisoformat(d)
    rows = _eyes_rows(
        "SELECT e.id, e.camera, e.started, e.ended, e.peak, e.snapshot, e.people, "
        "coalesce(json_agg(json_build_object('id', f.id, 'crop', f.crop, 'person', f.person, 'sim', f.similarity, "
        "'score', f.score, 'ts', f.ts) ORDER BY f.ts) FILTER (WHERE f.id IS NOT NULL), '[]') "
        "FROM eye_events e LEFT JOIN eye_faces f ON f.event_id = e.id "
        "WHERE (e.started AT TIME ZONE current_setting('TimeZone'))::date = %s AND (%s = '' OR e.camera = %s) "
        "GROUP BY e.id ORDER BY e.started DESC", (day, camera, camera))
    names = {x["id"]: x.get("name") or x["id"] for x in _jload(EYES_CONF, {}).get("cameras", [])}
    out = []
    for eid, cam, st, en, peak, snap, people, faces in rows:
        for f in faces:
            f["ts"] = _local(datetime.datetime.fromisoformat(f["ts"]))
        out.append({"id": eid, "camera": cam, "place": names.get(cam, cam), "start": _local(st), "end": _local(en),
                    "seconds": round((en - st).total_seconds()) if en else None, "peak": peak, "snapshot": snap,
                    "people": people or [], "faces": faces})
    return {"day": d, "events": out}


@app.get("/api/eyes/img/{path:path}")
def eyes_img(path: str):
    if not re.fullmatch(r"(events|faces)/\d{4}-\d{2}-\d{2}/[\w-]+\.jpg", path):
        raise HTTPException(404, "not found")
    p = os.path.join(EYES_DIR, path)
    if not os.path.exists(p):
        raise HTTPException(404, "gone")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# ---- camera scenarios (app/core/watchers.py reads /brain/watchers.json) ----
WTC = os.path.join(BRAIN, "watchers.json")
W_ON = ("motion", "person", "unknown", "quiet")
W_ACT = ("say", "telegram", "music", "eva", "log")


@app.get("/api/watchers")
def watchers_get():
    return {"watchers": _jload(WTC, {"watchers": []}).get("watchers", []), "log": _listener_events(kind="watcher")}


@app.post("/api/watchers")
async def watchers_post(request: Request):
    items = (await request.json()).get("watchers")
    if not isinstance(items, list):
        raise HTTPException(400, "watchers: []")
    seen = set()
    for x in items:
        x["id"] = re.sub(r"[^\w-]", "_", str(x.get("id") or os.urandom(3).hex()))[:40]
        if x["id"] in seen:
            raise HTTPException(400, f"id {x['id']} twice")
        seen.add(x["id"])
        if x.get("on") not in W_ON or x.get("action", "log") not in W_ACT:
            raise HTTPException(400, f"«{x.get('name') or x['id']}»: событие / действие")
        h = (x.get("hours") or "").replace(" ", "")
        if h and not re.fullmatch(r"\d{1,2}(:\d{2})?-\d{1,2}(:\d{2})?", h):
            raise HTTPException(400, f"«{x.get('name') or x['id']}»: время — как 22:00-07:00")
        x["hours"] = h
        if x.get("enabled") and x.get("on") == "motion" and x.get("action") in ("say", "eva", "music") \
                and not h and float(x.get("cooldown") or 0) < 60:
            # every passer-by would set it off: motion with a voice action needs a pause or hours
            raise HTTPException(400, f"«{x.get('name') or x['id']}»: на любое движение вслух — задай паузу от 60 с или время")
    _jsave(WTC, {"watchers": items})
    return watchers_get()
