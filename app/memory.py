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


def write(role, text, speaker=None, dialog=False, addressed=False, stt="gigaam", voice_sim=None, dur=None):
    """voice_sim / dur: how sure the speaker is - one short word was taken for
    Маша when she was not at home (2026-11-20); who_heard weighs them."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "role": role, "text": text}
    if role == "user":
        rec.update(speaker=speaker, dialog=bool(dialog), woke=bool(addressed), stt=stt)
        if voice_sim is not None:
            rec.update(voice_sim=round(float(voice_sim), 3), dur=round(float(dur or 0), 1))
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
                        "INSERT INTO utterances (ts, role, text, speaker, dialog, woke, stt)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (r.get("ts"), r.get("role"), r.get("text"), r.get("speaker"),
                         r.get("dialog"), r.get("woke"), r.get("stt")))
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
