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
            "text_heard": text_heard, "speaker_heard": speaker_heard, "corrected": corrected_at is not None}


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
            elif info.get("window") and not info.get("who"):
                info["who"] = [{"label": "во время речи Евы"}]
            hh = fn[:6]
            fix = info.get("correct") or {}
            if isinstance(fix, list):
                fix = next((f for f in fix if not f.get("span")), fix[-1] if fix else {})
            out.append({"id": None, "t": f"{hh[:2]}:{hh[2:4]}:{hh[4:6]}", "day": day, "role": "heard",
                        "speaker": fix.get("speaker") or _who(info), "text": fix.get("text") or info.get("text") or "",
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
    os.makedirs(KEEP_DIR, exist_ok=True)
    with open(SETTINGS + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(SETTINGS + ".tmp", SETTINGS)
    return s


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


@app.get("/api/listeners")
def listeners_get():
    items = _jload(LST, {"listeners": []}).get("listeners", [])
    fired = {}
    try:
        with db() as c:
            for ts, msg in c.execute("SELECT ts, msg FROM eva_log WHERE msg LIKE 'listener %%' "
                                     "AND ts > now() - interval '14 days' ORDER BY ts DESC LIMIT 200"):
                m = re.match(r"listener (\S+): (.*)", msg)
                if m:
                    fired.setdefault(m.group(1), []).append(
                        {"t": ts.astimezone().strftime("%d.%m %H:%M:%S"), "msg": m.group(2)[:200]})
    except Exception:
        pass
    return {"listeners": items, "log": {k: v[:10] for k, v in fired.items()}}


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
