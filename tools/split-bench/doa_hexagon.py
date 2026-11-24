#!/usr/bin/env python3
"""Snap the fitted mic positions (doa_geometry.py) to the array's real shape - a
regular hexagon of 6 mics + 1 in the centre - and check the angles of the test
set's positions with it. Prints the geometry json for the STT server.

    docker exec -i -w /app newalisa-stt python - < doa_hexagon.py   (needs /tmp/geometry.json)
"""
import glob
import json
import os
import sys
import wave

import numpy as np

import stt_server as S

g = json.load(open("/tmp/geometry.json"))
B, P = np.array(g["baselines"]), g["pairs"]
M = np.zeros((len(P), 7))
for k, (i, j) in enumerate(P):
    M[k, j] += 1
    M[k, i] -= 1
p = np.linalg.lstsq(M[:, 1:], B, rcond=None)[0]
p = np.vstack([[0, 0], p])
p -= p.mean(0)
r = np.linalg.norm(p, axis=1)
centre = int(np.argmin(r))
ring = [k for k in range(7) if k != centre]
ang = np.arctan2(p[ring, 1], p[ring, 0])
order = [ring[k] for k in np.argsort(ang)]
# a regular hexagon: angle_n = a0 + s * 60 n (s = +1 / -1 by the fitted order), radius R
radius = float(np.mean(r[ring]))
sorted_ang = np.sort(ang)
a0 = float(np.angle(np.mean(np.exp(1j * (sorted_ang - np.radians(60) * np.arange(6))))))
q = np.zeros((7, 2))
for n, k in enumerate(order):
    a = a0 + np.radians(60) * n
    q[k] = [radius * np.cos(a), radius * np.sin(a)]
err = float(np.sqrt(np.mean(np.sum((q - p) ** 2, 1))))
print(f"centre mic {centre}, ring order {order}, radius {radius:.3f} samples ({radius * 2.14:.2f} cm), "
      f"snap error {err:.3f} samples", file=sys.stderr)
Bh = np.array([q[j] - q[i] for i, j in P])

SR = 16000


def load7(path):
    with wave.open(path) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())
    return a.astype(np.float32) / 32768


files = sorted(glob.glob("/log/2026-11-23/*-raw7.wav"))
L = np.array([S.doa(load7(f))["lags"] for f in files])
for name, BB in (("fitted", B), ("hexagon", Bh)):
    u = np.linalg.lstsq(BB, L.T, rcond=None)[0].T
    az = np.degrees(np.arctan2(u[:, 1], u[:, 0]))
    t = [int(os.path.basename(f)[:2]) * 3600 + int(os.path.basename(f)[2:4]) * 60 + int(os.path.basename(f)[4:6])
         for f in files]
    groups, cur = [], [0]
    for k in range(1, len(t)):
        if t[k] - t[k - 1] > 15:
            groups.append(cur)
            cur = []
        cur.append(k)
    groups.append(cur)
    out = []
    for gr in groups:
        if len(gr) < 6:
            continue
        a = np.radians(az[gr])
        R = np.hypot(np.sin(a).mean(), np.cos(a).mean())
        out.append(f"{np.degrees(np.arctan2(np.sin(a).mean(), np.cos(a).mean())):6.1f}±{np.degrees(np.sqrt(-2 * np.log(R))):4.1f}"
                   f" |u|{np.median(np.linalg.norm(u[gr], axis=1)):.2f}")
    print(f"{name:8s}: " + "  ".join(out), file=sys.stderr)

json.dump({"pairs": P, "mics": np.round(q, 4).tolist(), "baselines": np.round(Bh, 4).tolist(),
           "radius_samples": round(radius, 4), "centre": centre, "ring": order}, sys.stdout)
