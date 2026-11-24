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


def _row(r):
    (uid, ts, role, speaker, text, dialog, woke, stt, audio, keep, text_heard, speaker_heard, corrected_at) = r
    base, frag = _clip(audio)
    return {"id": uid, "t": _local(ts), "day": ts.astimezone().strftime("%Y-%m-%d"), "role": role,
            "speaker": speaker, "text": text, "dialog": dialog, "woke": woke, "stt": stt,
            "audio": base, "frag": frag, "has_audio": bool(base and _path(base)), "keep": keep,
            "text_heard": text_heard, "speaker_heard": speaker_heard, "corrected": corrected_at is not None}


COLS = "id, ts, role, speaker, text, dialog, woke, stt, audio, keep, text_heard, speaker_heard, corrected_at"


@app.get("/")
def page():
    return HTMLResponse(open(os.path.join(HERE, "index.html"), encoding="utf-8").read())


@app.get("/api/days")
def days():
    with db() as c:
        rows = c.execute("SELECT (ts AT TIME ZONE current_setting('TimeZone'))::date d, count(*),"
                         " count(*) FILTER (WHERE keep), count(*) FILTER (WHERE audio IS NOT NULL)"
                         " FROM utterances GROUP BY 1 ORDER BY 1 DESC LIMIT 400").fetchall()
    out = {str(d): {"day": str(d), "n": n, "kept": k, "audio": a} for d, n, k, a in rows}
    if os.path.isdir(LOG_DIR):
        for d in os.listdir(LOG_DIR):
            out.setdefault(d, {"day": d, "n": 0, "kept": 0, "audio": 0})
    return sorted(out.values(), key=lambda x: x["day"], reverse=True)


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
            hh = fn[:6]
            fix = info.get("correct") or {}
            if isinstance(fix, list):
                fix = next((f for f in fix if not f.get("span")), fix[-1] if fix else {})
            out.append({"id": None, "t": f"{hh[:2]}:{hh[2:4]}:{hh[4:6]}", "day": day, "role": "heard",
                        "speaker": fix.get("speaker"), "text": fix.get("text") or info.get("text") or "",
                        "text_heard": info.get("text") if fix else None, "speaker_heard": None,
                        "corrected": bool(fix), "audio": base, "frag": "",
                        "has_audio": os.path.exists(os.path.join(d, fn[:-5] + ".wav")),
                        "keep": root == KEEP_DIR, "dur": info.get("duration")})
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


@app.post("/api/correct")
async def correct(request: Request):
    b = await request.json()
    text = (b.get("text") or "").strip()
    speaker = (b.get("speaker") or "").strip() or None
    if not text:
        raise HTTPException(400, "empty text: delete the phrase instead")
    if b.get("id"):
        uid = int(b["id"])
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
        return {"ok": True}
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
    _mark_clip(base, text, speaker, heard)
    return {"ok": True}
