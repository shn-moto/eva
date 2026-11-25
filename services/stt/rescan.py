"""Re-reading who said what, after the voices learnt more (on demand, from the
admin page). Nothing is changed here: the job gives proposals, a person looks
at them and the admin page applies the ones he keeps.

- a phrase in the memory that she labelled herself (not a person's correction)
  and that the voice base now gives to another family member, surely;
- a heard-only clip (a voice she did not know then, so not in the memory) that
  is now surely a family member's.

Same base and thresholds as the live recognition (voices.identify_many: no
learning, no guest groups). Each phrase's print is computed once - when the
hub hears it (hear.py), or here the first time it is missing - and kept in
voice_clip_prints: a rescan is then matrix arithmetic, not audio work (it took
a minute for 3 days and would have taken hours for a year, 2026-11-25).
"""
import json
import os
import threading
import time

SAME = 0.98          # a print this like the phrase is the phrase itself
SELF_MAX = 0.4       # this like Eva's own voice: her echo, not a person (the live echo rule: 0.45)
HEARD_MIN = 0.5      # a clip to bring into the memory: a sure match only, not the weaker "another room" one
JOB = {"state": "idle", "done": 0, "total": 0, "proposals": [], "started": None, "error": None, "secs": None}
LABEL = {"state": "idle", "done": 0, "total": 0, "labelled": 0}
_lock = threading.Lock()


def S():
    import stt_server
    return stt_server


def start(days):
    with _lock:
        if JOB["state"] == "running":
            return status()
        JOB.update(state="running", done=0, total=0, proposals=[], started=time.time(), error=None, secs=None)
    threading.Thread(target=_run, args=(int(days),), daemon=True).start()
    return status()


def status():
    with _lock:
        return dict(JOB, proposals=list(JOB["proposals"]))


def _run(days):
    try:
        _scan(days)
        JOB.update(state="done", secs=round(time.time() - JOB["started"], 1))
    except Exception as e:
        JOB.update(state="error", error=repr(e))


def _heard_clips(days, linked):
    """The STT log's clips of these days that no memory row points to."""
    st, out = S(), []
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
            if (info.get("text") or "").strip() and not info.get("joined_into") and not info.get("record") \
                    and not info.get("window") and not info.get("echo"):
                out.append((base, info))
    return out


def prints(keys, progress=None):
    """{audio key: (emb, dur)} from voice_clip_prints; the missing ones computed
    from the sound once and kept."""
    import psycopg
    st, vb = S(), S().VOICES
    have = {}
    with psycopg.connect(st.VOICE_DSN, connect_timeout=5) as c:
        for i in range(0, len(keys), 5000):
            for a, dur, emb in c.execute("SELECT audio, dur, emb FROM voice_clip_prints WHERE audio = ANY(%s)",
                                         (keys[i:i + 5000],)):
                have[a] = (emb, dur or 0.0)
    for k in keys:
        if k in have:
            continue
        e = st.clip_emb(k)
        if progress:
            progress()
        if "error" not in e:
            have[k] = (e["emb"], e["dur"])
            vb.store_print(k, e["emb"], e["dur"])
    return have


def _scan(days):
    import psycopg
    st, vb = S(), S().VOICES
    mark_echo(days)
    with psycopg.connect(st.VOICE_DSN, connect_timeout=5) as c:
        rows = c.execute(
            "SELECT id, ts, speaker, audio, text FROM utterances WHERE role = 'user' AND audio IS NOT NULL"
            " AND corrected_at IS NULL AND ts > now() - make_interval(days => %s) ORDER BY ts", (days,)).fetchall()
        linked = {a.split("#")[0] for (a,) in c.execute(
            "SELECT audio FROM utterances WHERE audio IS NOT NULL AND ts > now() - make_interval(days => %s)",
            (days + 1,)).fetchall()}
        # marked «не размечать» on the admin page: several voices, noise, the TV
        skip = {a for (a,) in c.execute("SELECT audio FROM voice_skip").fetchall()}
    rows = [r for r in rows if r[3] not in skip]
    heard = [(b, i) for b, i in _heard_clips(days, linked) if not i.get("correct") and b not in skip]
    keys = [r[3] for r in rows] + [b for b, _ in heard]
    JOB["total"] = len(keys)

    def tick():
        JOB["done"] += 1
    have = prints(keys, tick)
    JOB["done"] = JOB["total"]
    # one matrix pass over all of them
    rk = [r for r in rows if r[3] in have]
    hk = [(b, i) for b, i in heard if b in have]
    res = vb.identify_many([have[r[3]][0] for r in rk] + [have[b][0] for b, _ in hk],
                           [have[r[3]][1] for r in rk] + [have[b][1] for b, _ in hk], skip_same=SAME)
    selfs = vb.self_sims([have[b][0] for b, _ in hk])
    for (uid, ts, speaker, audio, text), (label, sim, known) in zip(rk, res[:len(rk)]):
        if known and label != speaker:
            JOB["proposals"].append({"kind": "retag", "id": uid, "ts": ts.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                                     "old": speaker, "new": label, "sim": sim, "text": text, "audio": audio})
    for ((base, info), (label, sim, known)), ss in zip(zip(hk, res[len(rk):]), selfs):
        if known and sim >= HEARD_MIN and ss < SELF_MAX:     # her own voice (echo) is not a family member's
            day, name = base.split("/")
            JOB["proposals"].append({"kind": "heard", "audio": base, "old": None, "new": label, "sim": sim,
                                     "ts": f"{day} {name[:2]}:{name[2:4]}:{name[4:6]}", "text": info.get("text")})


def _words(t):
    import re
    return re.findall(r"\w+", (t or "").lower().replace("ё", "е"))


def mark_echo(days=30):
    """Her own voice caught in the log (a window over her speech, an echo after
    it): a short clip whose words are her reply's words of the same minutes ->
    "echo" in its json (not labelled, not proposed, «Ева (эхо)» on the page).
    New windows are marked as they are heard; this finds the older ones."""
    import psycopg
    import datetime
    st = S()
    with psycopg.connect(st.VOICE_DSN, connect_timeout=5) as c:
        replies = c.execute("SELECT ts, text FROM utterances WHERE role = 'assistant'"
                            " AND ts > now() - make_interval(days => %s)", (days + 1,)).fetchall()
    replies = [(ts.timestamp(), set(_words(t))) for ts, t in replies]
    n = 0
    for day in sorted(os.listdir(st.LOG_DIR))[-days - 1:]:
        d = os.path.join(st.LOG_DIR, day)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json") or "-raw" in fn:
                continue
            p = os.path.join(d, fn)
            try:
                info = json.load(open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            words = _words(info.get("text"))
            if not words or info.get("echo") or info.get("correct") or (info.get("duration") or 99) > 4:
                continue
            t = datetime.datetime.strptime(day + fn[:6], "%Y-%m-%d%H%M%S").timestamp()
            near = set().union(*[w for ts, w in replies if -180 <= ts - t <= 120] or [set()])
            if near and sum(w in near for w in words) / len(words) >= 0.7:
                info["echo"] = True
                info.pop("who", None)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False)
                n += 1
    return n


def label_heard(days=30):
    """The voice group of every heard-only clip with none kept yet (existing
    groups only): the admin page shows strangers as «Неизвестный N»."""
    if LABEL["state"] == "running":
        return dict(LABEL)
    LABEL.update(state="running", done=0, total=0, labelled=0)

    def run():
        import hear
        vb = S().VOICES
        mark_echo(days)
        todo = [b for b, i in _heard_clips(days, set()) if not i.get("who")]
        LABEL["total"] = len(todo)

        def tick():
            LABEL["done"] += 1
        have = prints(todo, tick)
        keys = [b for b in todo if b in have]
        res = vb.identify_many([have[b][0] for b in keys], [have[b][1] for b in keys])
        guests = vb.guests_of([have[b][0] for b in keys])
        for b, (label, sim, known), (g, gsim) in zip(keys, res, guests):
            if not known and g:
                label, sim = g, gsim
            if label and label != "unknown":
                hear.note_who(b, None, label, sim)
                LABEL["labelled"] += 1
        LABEL.update(state="done", done=LABEL["total"])
    threading.Thread(target=run, daemon=True).start()
    return dict(LABEL)
