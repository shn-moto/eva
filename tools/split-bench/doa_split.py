#!/usr/bin/env python3
"""Can the direction (7 mics, /doa blocks) find a change of speaker inside a phrase?

Runs inside the STT container (it has doa()):
    docker exec -i newalisa-stt python - < doa_split.py  [LOG_DAY_DIR]

Data: the 7-channel test set (*-raw7.wav), recorded position by position - a
pause of > 15 s between positions gives the groups. Synthetic phrases:
- two phrases from DIFFERENT places glued: a change at the junction;
- two phrases from the SAME place glued: no change.
The same voice everywhere - exactly the case the voice cannot split.
"""
import glob
import itertools
import os
import random
import sys
import wave

import numpy as np

import stt_server as S

DAY = sys.argv[1] if len(sys.argv) > 1 else "/log/2026-11-23"
SR = 16000
BLOCK = S.DOA_BLOCK
MIN_SIDE = 1.0           # s of a part
random.seed(1)


def load7(p):
    with wave.open(p) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())
    return a.astype(np.float32) / 32768


def groups():
    files = sorted(glob.glob(os.path.join(DAY, "*-raw7.wav")))
    t = lambda f: (lambda s: int(s[:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6]))(os.path.basename(f))
    out, cur, last = [], [], None
    for f in files:
        if last is not None and t(f) - last > 15:
            out.append(cur)
            cur = []
        cur.append(f)
        last = t(f)
    out.append(cur)
    return [g for g in out if len(g) >= 6]


def cut_by_direction(blocks):
    """-> (distance, t) of the best cut: median fingerprint of the blocks left
    vs right; None if no cut leaves MIN_SIDE on both sides."""
    idx = [i for i, b in enumerate(blocks) if b is not None]
    B = {i: np.asarray(blocks[i]) for i in idx}
    best = (0.0, None)
    side = int(MIN_SIDE / BLOCK)
    for k in range(side, len(blocks) - side + 1):
        L = [B[i] for i in idx if i < k]
        R = [B[i] for i in idx if i >= k]
        if len(L) < 2 or len(R) < 2:
            continue
        d = float(np.mean(np.abs(np.median(L, 0) - np.median(R, 0))))
        if d > best[0]:
            best = (d, k * BLOCK)
    return best


def frames(m):
    """-> X (voiced frames x bins x mics rfft), frame times, pairs - as doa() does."""
    nmic = m.shape[1]
    pairs = [(i, j) for i in range(nmic) for j in range(i + 1, nmic)]
    win = np.hanning(S.DOA_N)[:, None]
    starts = list(range(0, len(m) - S.DOA_N + 1, S.DOA_HOP))
    F = np.stack([m[s:s + S.DOA_N] * win for s in starts])
    energy = (F ** 2).sum((1, 2))
    voiced = energy >= np.percentile(energy, 50)
    X = np.fft.rfft(F, axis=1)
    X[:, :int(200 * S.DOA_N / SR)] = 0
    t = (np.asarray(starts) + S.DOA_N / 2) / SR
    return X[voiced], t[voiced], pairs


def cut_pooled(m, step=0.25):
    """Each side's fingerprint from all its voiced frames at once (like the
    whole phrase's), the cut where they differ most."""
    X, t, pairs = frames(m)
    best = (0.0, None)
    end = len(m) / SR
    c = MIN_SIDE
    while c <= end - MIN_SIDE:
        L, R = X[t < c], X[t >= c]
        if len(L) >= 8 and len(R) >= 8:
            d = float(np.mean(np.abs(np.asarray(S._gcc(L, pairs)[0]) - np.asarray(S._gcc(R, pairs)[0]))))
            if d > best[0]:
                best = (d, c)
        c += step
    return best


def curves(X, pairs):
    """GCC-PHAT curves of all pairs (pairs x lags), each unit length."""
    n = (X.shape[1] - 1) * 2
    m = S.DOA_MAXLAG * S.DOA_UP
    out = []
    for i, j in pairs:
        c = (X[:, :, i] * np.conj(X[:, :, j])).sum(0)
        c = c / (np.abs(c) + 1e-12)
        r = np.fft.irfft(c, n * S.DOA_UP)
        r = np.concatenate([r[-m:], r[:m + 1]])
        r = r - r.mean()
        out.append(r / (np.linalg.norm(r) + 1e-12))
    return np.stack(out)


def cut_curves(m, step=0.25):
    X, t, pairs = frames(m)
    best = (0.0, None)
    end = len(m) / SR
    c = MIN_SIDE
    while c <= end - MIN_SIDE:
        L, R = X[t < c], X[t >= c]
        if len(L) >= 8 and len(R) >= 8:
            d = 1.0 - float(np.mean((curves(L, pairs) * curves(R, pairs)).sum(1)))
            if d > best[0]:
                best = (d, c)
        c += step
    return best


def report(name, rows, thrs=(0.3, 0.4, 0.5, 0.6, 0.7, 0.8)):
    same = [d for gi, gj, d, t, j in rows if gi == gj]
    diff = [(gi, gj, d, t, j) for gi, gj, d, t, j in rows if gi != gj]
    print(f"== {name}: same place p50 {np.median(same):.3f} p90 {np.percentile(same, 90):.3f} max {max(same):.3f}"
          f" | other place p10 {np.percentile([x[2] for x in diff], 10):.3f} p50 {np.median([x[2] for x in diff]):.3f}")
    for thr in thrs:
        fp = sum(d >= thr for d in same) / len(same)
        tp = [x for x in diff if x[2] >= thr]
        err = [abs(x[3] - x[4]) for x in tp]
        print(f"  thr {thr:.2f}: false split {fp:5.1%}  found {len(tp) / len(diff):5.1%}  "
              f"cut error p50 {np.median(err) if err else 0:.2f}s p90 {np.percentile(err, 90) if err else 0:.2f}s")


def main():
    G = groups()
    print("groups:", [len(g) for g in G])
    clips = [[load7(f) for f in g] for g in G]
    pooled = []
    for gi, gj in itertools.product(range(len(G)), repeat=2):
        for _ in range(12):
            a = random.choice(clips[gi])
            b = random.choice([c for c in clips[gj] if c is not a] or clips[gj])
            d, t = cut_curves(np.concatenate([a, b]))
            pooled.append((gi, gj, d, t, len(a) / SR))
    report("GCC curves", pooled, (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4))
    print("per pair of places (median distance):")
    for gi in range(len(G)):
        print("  ", " ".join(f"{np.median([d for a, b, d, t, j in pooled if a == gi and b == gj]):.2f}"
                             for gj in range(len(G))))
    return


def main_blocks():
    G = groups()
    print("groups:", [len(g) for g in G])
    clips = [[load7(f) for f in g] for g in G]
    rows = []
    for gi, gj in itertools.product(range(len(G)), repeat=2):
        for _ in range(12):
            a = random.choice(clips[gi])
            b = random.choice([c for c in clips[gj] if c is not a] or clips[gj])
            m = np.concatenate([a, b])
            r = S.doa(m)
            d, t = cut_by_direction(r["blocks"])
            rows.append((gi, gj, d, t, len(a) / SR))
    same = [d for gi, gj, d, t, j in rows if gi == gj]
    diff = [(gi, gj, d, t, j) for gi, gj, d, t, j in rows if gi != gj]
    print(f"same place: n={len(same)} distance p50 {np.median(same):.3f} p90 {np.percentile(same, 90):.3f} max {max(same):.3f}")
    dd = [x[2] for x in diff]
    print(f"other place: n={len(diff)} distance p10 {np.percentile(dd, 10):.3f} p50 {np.median(dd):.3f}")
    for thr in (0.2, 0.25, 0.3, 0.35, 0.4, 0.5):
        fp = sum(d >= thr for d in same) / len(same)
        tp = [x for x in diff if x[2] >= thr]
        err = [abs(x[3] - x[4]) for x in tp]
        print(f"thr {thr:.2f}: false split {fp:5.1%}  found {len(tp) / len(diff):5.1%}  "
              f"cut error p50 {np.median(err) if err else 0:.2f}s p90 {np.percentile(err, 90) if err else 0:.2f}s")
    print("per pair of places (median distance):")
    for gi in range(len(G)):
        print("  ", " ".join(f"{np.median([d for a, b, d, t, j in rows if a == gi and b == gj]):.2f}"
                             for gj in range(len(G))))


main()
