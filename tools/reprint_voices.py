"""Correction voice prints rebuilt from their clips' raw 7 mics through the ear chain as it is now
(level -> high-pass -> WebRTC AEC3+NS -> MIC_GAIN), for when the chain changes and the old prints
no longer match what is heard (2026-11-26: the processing moved to the hub, mics 20 -> 30 dB).

Runs inside newalisa-stt:  docker cp tools/reprint_voices.py newalisa-stt:/tmp/ &&
                           docker exec -w /app newalisa-stt python /tmp/reprint_voices.py [--apply]
Prints a leave-one-out check: each rebuilt phrase (its first 3 s, as live) against the old and the new
base. --apply: the whole table copied to voice_prints_backup_<date> first, then the new embeddings
written; restart the STT service to load them. Only "correction" prints have their clip linked;
enroll / auto / cluster prints came from the Station without it and stay as they are.

2026-11-26: 79 rebuilt; recognised 35 -> 38 of 79, wrong person 3 -> 1, one voice 0 -> 7 of 14;
two sisters came closer (top-1 right, lead < 0.1 more often)."""
import json
import os
import sys
import time
import wave

import numpy as np
import psycopg

sys.path.insert(0, "/app")
import ear_stream as es          # noqa: E402
import stt_server as st          # noqa: E402

LOG = "/log"
TARGET = -43.0                   # dBFS of speech at the APM's input with the mics at 30 dB (21:45 run)
APPLY = "--apply" in sys.argv


def frames_db(x, n=512):
    k = len(x) // n
    e = (x[:k * n].reshape(k, n) ** 2).mean(1)
    return 10 * np.log10(e + 1e-20)


def chain(base):
    w = wave.open(os.path.join(LOG, base + "-mics.wav"))
    m = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, w.getnchannels()).astype(np.float64)
    mono = m[:, es.HEAR_MICS].mean(axis=1)
    lvl = float(np.percentile(frames_db(mono / 32768), 95))
    g = 10 ** ((TARGET - lvl) / 20)
    g = max(0.1, min(g, 10.0, 0.9 * 32768 / (np.abs(mono).max() + 1e-9)))
    mono = mono * g
    f = es.Front()
    both = np.concatenate([mono, mono])          # the first pass lets the noise suppressor learn the room
    out = np.concatenate([f.process(both[i:i + 256], np.zeros(256, np.int16))
                          for i in range(0, len(both) - 255, 256)]).astype(np.float64)
    seg = out[-len(mono):] * es.MIC_GAIN / 32768 if len(out) >= len(mono) else out * es.MIC_GAIN / 32768
    return seg.astype(np.float32), round(20 * np.log10(g), 1)


def part(seg, audio, info):
    """As clip_emb: a mixed phrase -> its clean head; a span -> the span."""
    _, _, span = audio.partition("#t=")
    if info.get("mixed") is not None:
        head = info.get("head")
        if span or not head:
            return None
        return seg[:int(head * 16000)]
    if span:
        p = [float(x) for x in span.split(",")]
        return seg[int(p[0] * 16000): int(p[1] * 16000) if len(p) > 1 else None]
    return seg


with psycopg.connect(os.environ["ASSISTANT_PG_DSN"]) as c:
    rows = c.execute("SELECT id, speaker, source, audio, emb FROM voice_prints ORDER BY id").fetchall()

base_old = {pid: (spk, src, np.asarray(e, np.float32) / np.linalg.norm(e)) for pid, spk, src, audio, e in rows}
new, test = {}, {}
t0 = time.time()
for pid, spk, src, audio, e in rows:
    if src != "correction" or not audio:
        continue
    b = audio.split("#")[0]
    if not os.path.exists(os.path.join(LOG, b + "-mics.wav")):
        continue
    try:
        info = json.load(open(os.path.join(LOG, b + ".json"), encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    seg, gdb = chain(b)
    p = part(seg, audio, info)
    if p is None or len(p) < 8000:
        continue
    new[pid] = st.spk_emb(p)
    test[pid] = st.spk_emb(p[:int(16000 * 3.0)])        # as live: the first 3 s
print(f"rebuilt {len(new)} prints in {time.time() - t0:.0f} s")

ME = "Ева"                       # her own voice prints (source self) are not a person


def score(base, t, skip):
    best = {}
    for pid, (spk, src, e) in base.items():
        if pid == skip or spk == ME:
            continue
        s = float(t @ e)
        if s > best.get(spk, -1):
            best[spk] = s
    r = sorted(((s, k) for k, s in best.items()), reverse=True)
    return r


def verdict(r, truth):
    top, name = r[0]
    second = r[1][0] if len(r) > 1 else 0
    known = top >= 0.5 and top - second >= 0.1
    return known and name == truth, known and name != truth, top if name == truth else None, top - second


base_new = dict(base_old)
for pid, e in new.items():
    spk, src, _ = base_old[pid]
    base_new[pid] = (spk, src, e)

base_both = dict(base_old)
for pid, e in new.items():
    spk, src, _ = base_old[pid]
    base_both[-pid] = (spk, src, e)                 # beside the old one, its own id


def score_both(t, skip):
    return score({k: v for k, v in base_both.items() if abs(k) != skip}, t, None)


for label, base in (("old base", base_old), ("new base", base_new), ("both", None)):
    ok = wrong = 0
    sims, by = [], {}
    for pid, t in test.items():
        truth = base_old[pid][0]
        r = score(base, t, pid) if base is not None else score_both(t, pid)
        good, bad, s, lead = verdict(r, truth)
        ok += good
        wrong += bad
        d = by.setdefault(truth, [0, 0])
        d[0] += good
        d[1] += 1
        if s is not None:
            sims.append(s)
    print(f"{label}: recognised {ok}/{len(test)}, WRONG person {wrong}, median sim to the right one "
          f"{np.median(sims):.3f}; per person " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by.items())))

if APPLY:
    with psycopg.connect(os.environ["ASSISTANT_PG_DSN"]) as c:
        bk = "voice_prints_backup_" + time.strftime("%Y%m%d")
        c.execute(f"CREATE TABLE IF NOT EXISTS {bk} AS SELECT * FROM voice_prints")
        for pid, e in new.items():
            c.execute("UPDATE voice_prints SET emb = %s WHERE id = %s", (e.round(5).tolist(), pid))
        c.commit()
    print(f"applied: {len(new)} prints rebuilt, the old ones in {bk}")
