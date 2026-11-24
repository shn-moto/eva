#!/usr/bin/env python3
"""Fit the Station's mic array geometry from recordings - no datasheet needed.

A far voice reaches mic i and mic j with a delay  lag_ij = b_ij . u  (samples),
u = the direction on the array's plane, b_ij = the pair's baseline (mic j - mic i,
in samples at 16 kHz). The 21 lags of N phrases form an N x 21 matrix of rank 2:
an SVD gives u up to a 2x2 transform; the constraint |u| = 1 (voices roughly at
the array's height) fixes it up to a rotation / mirror, which the admin page
sets from one known place ("in front of the Station").

    docker exec -i -w /app newalisa-stt python - < doa_geometry.py [RAW7_DIR ...] > geometry.json

Writes {"pairs": [[i, j]...], "baselines": [[bx, by] x 21], "fit": {...}} to stdout
and prints a check on the test set's positions to stderr.
"""
import glob
import json
import os
import sys
import wave

import numpy as np

import stt_server as S

SR = 16000
DIRS = sys.argv[1:] or ["/log/2026-11-23"]


def load7(p):
    with wave.open(p) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())
    return a.astype(np.float32) / 32768


def main():
    files = sorted(f for d in DIRS for f in glob.glob(os.path.join(d, "*-raw7.wav")))
    L, names, peaks = [], [], []
    for f in files:
        r = S.doa(load7(f))
        if r:
            L.append(r["lags"])
            peaks.append(r["peak"])
            names.append(os.path.basename(f))
    L = np.asarray(L, dtype=np.float64)
    nmic = 7
    pairs = [(i, j) for i in range(nmic) for j in range(i + 1, nmic)]
    keep = np.asarray(peaks) >= np.percentile(peaks, 20)      # the clearest phrases fit the geometry
    U, s, Vt = np.linalg.svd(L[keep], full_matrices=False)
    print(f"phrases {len(L)} (fit on {keep.sum()}), singular values {np.round(s[:5], 2)}"
          f" - rank 2 share {float((s[:2] ** 2).sum() / (s ** 2).sum()):.1%}", file=sys.stderr)
    # L ~ W @ B.T with W = U[:, :2] * s[:2] (n x 2), B = Vt[:2].T (21 x 2); u = W @ A, b = B @ inv(A).T
    W = U[:, :2] * s[:2]
    # |W a|^2 = 1 for every phrase: w^T Q w = 1, Q = A A^T symmetric -> 3 unknowns, least squares
    M = np.stack([W[:, 0] ** 2, 2 * W[:, 0] * W[:, 1], W[:, 1] ** 2], 1)
    q = np.linalg.lstsq(M, np.ones(len(W)), rcond=None)[0]
    Q = np.array([[q[0], q[1]], [q[1], q[2]]])
    ev = np.linalg.eigvalsh(Q)
    if ev.min() <= 0:
        print(f"Q not positive {ev} - directions too alike to fit", file=sys.stderr)
        sys.exit(1)
    A = np.linalg.cholesky(Q)                                 # Q = A A^T
    B = Vt[:2].T @ np.linalg.inv(A).T                         # baselines in samples: lag = B @ u
    # check: the angle of every phrase (least squares on its lags), spread per test position
    u = np.linalg.lstsq(B, L.T, rcond=None)[0].T
    az = np.degrees(np.arctan2(u[:, 1], u[:, 0]))
    norm = np.linalg.norm(u, axis=1)
    t = [int(n[:2]) * 3600 + int(n[2:4]) * 60 + int(n[4:6]) for n in names]
    groups, cur = [], [0]
    for k in range(1, len(t)):
        if t[k] - t[k - 1] > 15:
            groups.append(cur)
            cur = []
        cur.append(k)
    groups.append(cur)
    for g in groups:
        if len(g) < 6:
            continue
        a = np.radians(az[g])
        mean = np.degrees(np.arctan2(np.sin(a).mean(), np.cos(a).mean()))
        spread = np.degrees(np.sqrt(-2 * np.log(np.hypot(np.sin(a).mean(), np.cos(a).mean()))))
        print(f"position of {len(g):2d} phrases: angle {mean:7.1f}  spread {spread:5.1f}  |u| {np.median(norm[g]):.2f}",
              file=sys.stderr)
    resid = float(np.sqrt(np.mean((L - u @ B.T) ** 2)))
    print(f"lag residual {resid:.3f} samples (lags span +-{np.abs(L).max():.1f})", file=sys.stderr)
    json.dump({"pairs": pairs, "baselines": np.round(B, 4).tolist(),
               "fit": {"phrases": int(keep.sum()), "residual": round(resid, 3),
                       "rank2": round(float((s[:2] ** 2).sum() / (s ** 2).sum()), 4)}}, sys.stdout)


main()
