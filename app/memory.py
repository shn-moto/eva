"""Long-term memory: everything said to and by Eva, kept for good.

- transcript: JSONL per day on the Station (/var/lib/eva/transcript), written
  first, so nothing is lost while the database is away;
- shipper: tails the transcript into Postgres assistant_memory.utterances on
  shn-linux (a bookmark on disk survives restarts, ON CONFLICT DO NOTHING);
  there the indexer segments, corrects and embeds it;
- search: semantic search over it (shn-linux :8903), Claude's memory_search tool.

Format and table are the old assistant's (2026-11-04), so the old memory and
the new one are one.
"""
import json
import os
import threading
import time
import urllib.request

from core import profile

DIR = "/var/lib/eva/transcript"
STATE = "/var/lib/eva/shipper.json"
SEARCH_URL = profile.service("memory")
_lock = threading.Lock()


def write(role, text, speaker=None, dialog=False, addressed=False, stt="gigaam", voice_sim=None, dur=None,
          audio=None, audio_span=None):
    """voice_sim / dur: how sure the speaker is - one short word was taken for
    Маша when she was not at home (2026-11-20); who_heard weighs them.
    audio: the phrase's sound kept on the hub ("<date>/<time>", STT log), with
    audio_span [from, to] seconds when the phrase is a part of that clip."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "role": role, "text": text}
    if speaker:
        _activity[(speaker, "telegram" if stt == "telegram" else "voice")] = time.time()
    if role == "assistant":
        # whom she answered and where: the other channel finds her words by them
        # (recent()) - without it "what she told Саша by voice" was not in the base
        if speaker:
            rec.update(speaker=speaker, stt=stt)
    elif role == "user":
        rec.update(speaker=speaker, dialog=bool(dialog), woke=bool(addressed), stt=stt)
        if voice_sim is not None:
            rec.update(voice_sim=round(float(voice_sim), 3), dur=round(float(dur or 0), 1))
        if audio:
            rec["audio"] = audio + (f"#t={audio_span[0]:g},{audio_span[1]:g}" if audio_span and audio_span[1]
                                    else f"#t={audio_span[0]:g}" if audio_span and audio_span[0] else "")
    try:
        os.makedirs(DIR, exist_ok=True)
        with _lock, open(os.path.join(DIR, time.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print("transcript write error:", e, flush=True)


def shipper(dsn, log):
    """Transcript tail -> Postgres. Runs forever; catches up after an outage."""
    import psycopg
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
                # utterances.audio / .keep (the admin page, services/admin) are added by
                # the table's owner once - this role may not ALTER (2026-11-24):
                #   ALTER TABLE utterances ADD COLUMN IF NOT EXISTS audio text;
                #   ALTER TABLE utterances ADD COLUMN IF NOT EXISTS keep boolean NOT NULL DEFAULT false;
                log("memory: connected to assistant_memory")
            try:
                state = json.load(open(STATE, encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            moved = False
            for fn in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(DIR, fn)
                size, off = os.path.getsize(path), state.get(fn, 0)
                if off > size:
                    off = 0
                if off == size:
                    continue
                with open(path, encoding="utf-8") as f:
                    f.seek(off)
                    lines = f.readlines()
                    new_off = f.tell()
                for line in lines:
                    if not line.endswith("\n"):           # half-written last line: next round
                        new_off -= len(line.encode("utf-8"))
                        break
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    conn.execute(
                        "INSERT INTO utterances (ts, role, text, speaker, dialog, woke, stt, audio)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (r.get("ts"), r.get("role"), r.get("text"), r.get("speaker"),
                         r.get("dialog"), r.get("woke"), r.get("stt"), r.get("audio")))
                state[fn] = new_off
                moved = True
            if moved:
                tmp = STATE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f)
                os.replace(tmp, STATE)
            time.sleep(5)
        except Exception as e:  # database or network away: retry later
            log(f"memory: {e}")
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(15)


def search(query, limit=6):
    req = urllib.request.Request(SEARCH_URL, data=json.dumps({"query": query, "limit": limit}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            hits = json.loads(r.read()).get("results", [])
    except OSError as e:
        return f"Память недоступна: {e}"
    if not hits:
        return "Ничего похожего в памяти не нашлось."
    return "\n".join(f"[{h.get('ts')}] {h.get('text')}" for h in hits)


# ---- short-term memory across channels, from the base --------------------------------------
# Voice and Telegram keep their own conversation (with the tool calls and their
# results), but what was said with the same person in the other channel comes
# from the common log in the base: she was asked by voice to look up tyre prices,
# then sent the photos in Telegram, and there she knew nothing of it (2026-11-24).
RECENT_HOURS = profile.tune("memory.recent_hours", 6)
RECENT_CHARS = profile.tune("memory.recent_chars", 4000)
_dsn = None
_activity = {}          # (profile, channel) -> when something was last said there (write())


def _channel(stt):
    return "telegram" if stt == "telegram" else "voice"


def recent(person_profile, channel, since=None, name=None, hours=RECENT_HOURS):
    """What was said with this person in the OTHER channel since `since` (unix
    time, else `hours` back): -> (lines as text, the newest ts) or ("", since).
    Read from the base; when it is away - nothing, the answer still goes."""
    global _dsn
    if not person_profile:
        return "", since
    # the base is asked only when the other channel had something with this person
    # since the last look (both channels live in this process), or the first time
    # after a start: no round trip before every spoken answer
    other_ch = "voice" if channel == "telegram" else "telegram"
    last = _activity.get((person_profile, other_ch))
    if since is not None and (last is None or last <= since):
        return "", since
    try:
        import datetime
        import psycopg
        from core.util import secrets
        _dsn = _dsn or secrets().get("ASSISTANT_PG_DSN")
        if not _dsn:
            return "", since
        start = datetime.datetime.fromtimestamp(max(since or 0, time.time() - hours * 3600)).astimezone()
        with psycopg.connect(_dsn, connect_timeout=3) as c:
            rows = c.execute(
                "SELECT ts, role, stt, text FROM utterances WHERE ts > %s AND speaker = %s"
                " ORDER BY ts", (start, person_profile)).fetchall()
    except Exception as e:
        print("memory recent:", e, flush=True)
        return "", since
    other = [r for r in rows if _channel(r[2]) != channel and r[3]]
    if not other:
        return "", since
    where = "в Телеграме" if channel == "voice" else "голосом"
    lines = []
    for ts, role, stt, text in other:
        who = "Ева" if role == "assistant" else (name or person_profile)
        lines.append(f"{ts.astimezone():%H:%M} {who}: {text.strip()}")
    text = "\n".join(lines)
    if len(text) > RECENT_CHARS:
        text = "…" + text[-RECENT_CHARS:]
    return f"[недавно {where} с этим же человеком (из общей памяти):\n{text}]", other[-1][0].timestamp()
