#!/usr/bin/env python3
"""Did the words go to the right part? Two test phrases from two places glued,
the deployed /doa + /stt?split=1, the parts' text next to each phrase's own.

    docker exec -i -w /app newalisa-stt python - < split_words.py
"""
import glob
import json
import os
import random
import urllib.request
import wave

import numpy as np

DAY = "/log/2026-11-23"
STT = "http://127.0.0.1:8901"
ADMIN = os.environ.get("ADMIN", "http://192.168.1.107:8908")
random.seed(11)


def load7(p):
    with wave.open(p) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())


def mono(raw):
    m = raw[:, [0, 2, 4]].astype(np.float32).mean(1)
    return (m / (np.abs(m).max() + 1) * 0.9 * 32767).astype(np.int16)


def post(url, body, hdr):
    req = urllib.request.Request(url, data=body, headers=dict(hdr, **{"Content-Type": "application/octet-stream"}))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


files = sorted(glob.glob(os.path.join(DAY, "*-raw7.wav")))
t = lambda f: (lambda s: int(s[:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6]))(os.path.basename(f))
G, cur, last = [], [], None
for f in files:
    if last is not None and t(f) - last > 15:
        G.append(cur)
        cur = []
    cur.append(f)
    last = t(f)
G = [[load7(f) for f in g] for g in G + [cur] if len(g) >= 6]
made, ok, n = [], 0, 0
for gi, gj in [(0, 1), (1, 2), (2, 4), (3, 1), (4, 2), (1, 3), (0, 2), (2, 1)]:
    for _ in range(2):
        a, b = random.choice(G[gi]), random.choice(G[gj])
        ta, tb = (post(STT + "/stt", mono(x).tobytes(), {}) for x in (a, b))
        made += [x["audio"] for x in (ta, tb) if x.get("audio")]
        raw = np.concatenate([a, b])
        pid = f"words-{gi}{gj}-{random.random():.6f}"
        post(STT + "/doa", raw.astype("<i2").tobytes(), {"X-Channels": "7", "X-Phrase": pid})
        r = post(STT + "/stt?split=1", mono(raw).tobytes(), {"X-Phrase": pid})
        if r.get("audio"):
            made.append(r["audio"])
        parts = r.get("parts") or []
        n += 1
        good = len(parts) == 2 and parts[0]["text"] == ta["text"] and parts[1]["text"] == tb["text"]
        ok += good
        print(f"{'OK ' if good else '-- '} A: {ta['text']!r} | B: {tb['text']!r}")
        print("     " + (" | ".join(f"{p['from']}-{p['to']}s {p['text']!r}" for p in parts) if parts else f"not cut: {r['text']!r}"))
print(f"words exactly in their parts: {ok} of {n}")
for x in made:
    urllib.request.urlopen(urllib.request.Request(ADMIN + "/api/delete", data=json.dumps({"audio": x}).encode(),
                                                  headers={"Content-Type": "application/json"}), timeout=10).read()
print(f"cleaned {len(made)} test clips")
