"""Re-reading who said what, after the voices learnt more (on demand, from the
admin page). Nothing is changed here: the job gives proposals, a person looks
at them and the admin page applies the ones he keeps.

- a phrase in the memory that she labelled herself (not a person's correction)
  and that the voice base now gives to another family member, surely;
- a heard-only clip (a voice she did not know then, so not in the memory) that
  is now surely a family member's.

Same base and thresholds as the live recognition (voices.identify, learn=False:
nothing is learnt, no guest clusters). The print is made of the whole phrase
(or its part), not of its first 3 s as live.
"""
import json
import os
import threading
import time

SAME = 0.98          # a print this like the phrase is the phrase itself
SELF_MAX = 0.4       # this like Eva's own voice: her echo, not a person (the live echo rule: 0.45)
HEARD_MIN = 0.5      # a clip to bring into the memory: a sure match only, not the weaker "another room" one
JOB = {"state": "idle", "done": 0, "total": 0, "proposals": [], "started": None, "error": None}
_lock = threading.Lock()


def S():
    import stt_server
    return stt_server


def start(days):
    with _lock:
        if JOB["state"] == "running":
            return status()
        JOB.update(state="running", done=0, total=0, proposals=[], started=time.time(), error=None)
    threading.Thread(target=_run, args=(int(days),), daemon=True).start()
    return status()


LABEL = {"state": "idle", "done": 0, "total": 0, "labelled": 0}


def label_heard(days=30):
    """Once, for the clips heard before their group was kept with them: the
    guest group each heard-only clip belongs to (existing groups only)."""
    if LABEL["state"] == "running":
        return dict(LABEL)
    LABEL.update(state="running", done=0, total=0, labelled=0)

    def run():
        import hear
        st, vb = S(), S().VOICES
        todo = []
        for day in sorted(os.listdir(st.LOG_DIR))[-days - 1:]:
            d = os.path.join(st.LOG_DIR, day)
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".json") or "-raw" in fn:
                    continue
                try:
                    info = json.load(open(os.path.join(d, fn), encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (info.get("text") or "").strip() and not info.get("who") and not info.get("record"):
                    todo.append(f"{day}/{fn[:-5]}")
        LABEL["total"] = len(todo)
        for base in todo:
            e = st.clip_emb(base)
            LABEL["done"] += 1
            if "error" in e:
                continue
            label, sim, known = vb.identify(e["emb"], e["dur"], learn=False, skip_same=None)
            if not known:
                g, gsim = vb.guest_of(e["emb"])
                if g:
                    label, sim = g, gsim
            if label and label != "unknown":
                hear.note_who(base, None, label, sim)
                LABEL["labelled"] += 1
        LABEL["state"] = "done"
    threading.Thread(target=run, daemon=True).start()
    return dict(LABEL)


def status():
    with _lock:
        return dict(JOB, proposals=list(JOB["proposals"]))


def _run(days):
    try:
        _scan(days)
        JOB["state"] = "done"
    except Exception as e:
        JOB.update(state="error", error=repr(e))


def _scan(days):
    import psycopg
    st, vb = S(), S().VOICES
    with psycopg.connect(st.VOICE_DSN, connect_timeout=5) as c:
        rows = c.execute(
            "SELECT id, ts, speaker, audio, text FROM utterances WHERE role = 'user' AND audio IS NOT NULL"
            " AND corrected_at IS NULL AND ts > now() - make_interval(days => %s) ORDER BY ts", (days,)).fetchall()
        linked = {a.split("#")[0] for (a,) in c.execute(
            "SELECT audio FROM utterances WHERE audio IS NOT NULL AND ts > now() - make_interval(days => %s)",
            (days + 1,)).fetchall()}
    heard = []
    for day in sorted(os.listdir(st.LOG_DIR))[-days - 1:]:
        d = os.path.join(st.LOG_DIR, day)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json") or "-raw" in fn:
                continue
            base = f"{day}/{fn[:-5]}"
            if base in linked:
                continue
            try:
                info = json.load(open(os.path.join(d, fn), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (info.get("text") or "").strip() and not info.get("joined_into") and not info.get("correct") \
                    and not info.get("record"):
                heard.append((base, info))
    JOB["total"] = len(rows) + len(heard)
    for uid, ts, speaker, audio, text in rows:
        e = st.clip_emb(audio)
        JOB["done"] += 1
        if "error" in e:
            continue
        # its own print (learnt from this very phrase) does not count: a match of 1.0 proves nothing
        label, sim, known = vb.identify(e["emb"], e["dur"], learn=False, skip_same=SAME)
        if known and label != speaker:
            JOB["proposals"].append({"kind": "retag", "id": uid, "ts": ts.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                                     "old": speaker, "new": label, "sim": sim, "text": text, "audio": audio})
    for base, info in heard:
        e = st.clip_emb(base)
        JOB["done"] += 1
        if "error" in e:
            continue
        # her own voice (her echo, «сейчас посмотрю») is not a family member's
        if vb.self_sim(e["emb"]) >= SELF_MAX:
            continue
        label, sim, known = vb.identify(e["emb"], e["dur"], learn=False, skip_same=SAME)
        if known and sim >= HEARD_MIN:
            day, name = base.split("/")
            JOB["proposals"].append({"kind": "heard", "audio": base, "old": None, "new": label, "sim": sim,
                                     "ts": f"{day} {name[:2]}:{name[2:4]}:{name[4:6]}", "text": info.get("text")})
