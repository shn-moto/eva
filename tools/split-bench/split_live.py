#!/usr/bin/env python3
"""The deployed splitter end to end: /doa + /stt?split=1 with one X-Phrase id, on
test-set phrases glued from two places (a cut expected) or one place (none).

    docker exec -i -w /app newalisa-stt python - < split_live.py

The clips this leaves in the STT log are deleted through the admin page's API.
"""
import glob
import itertools
import json
import os
import random
import urllib.request
import wave

import numpy as np

DAY = "/log/2026-11-23"
STT = "http://127.0.0.1:8901"
ADMIN = os.environ.get("ADMIN", "http://192.168.1.107:8908")
random.seed(7)


def load7(p):
    with wave.open(p) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())


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

rows, made = [], []
n = 0
for gi, gj in itertools.product(range(len(G)), repeat=2):
    for _ in range(3 if gi != gj else 6):
        a = random.choice(G[gi])
        b = random.choice([c for c in G[gj] if c is not a] or G[gj])
        raw = np.concatenate([a, b])
        mono = raw[:, [0, 2, 4]].astype(np.float32).mean(1)
        mono = (mono / (np.abs(mono).max() + 1) * 0.9 * 32767).astype(np.int16)
        pid = f"test-{n}"
        n += 1
        d = post(STT + "/doa", raw.astype("<i2").tobytes(), {"X-Channels": "7", "X-Phrase": pid})
        r = post(STT + "/stt?split=1", mono.tobytes(), {"X-Phrase": pid})
        if r.get("audio"):
            made.append(r["audio"])
        parts = r.get("parts") or []
        cut_true = len(a) / 16000
        cuts = [p["cut"] for p in parts if p.get("cut")]
        rows.append((gi, gj, cuts, cut_true, d.get("az"), [p.get("az") for p in parts], r.get("text", "")[:50]))

same = [x for x in rows if x[0] == x[1]]
diff = [x for x in rows if x[0] != x[1]]
fp = sum(1 for x in same if any(c["by"] == "direction" for c in x[2]))
found = [x for x in diff if any(c["by"] == "direction" for c in x[2])]
err = [min(abs(c["t"] - x[3]) for c in x[2] if c["by"] == "direction") for x in found]
print(f"one place:  {len(same)} glued, direction cut in {fp}")
print(f"two places: {len(diff)} glued, direction cut found in {len(found)}"
      f" ({len(found) / len(diff):.0%}), cut error p50 {np.median(err):.2f}s max {max(err):.2f}s")
for x in rows[:8] + rows[-4:]:
    print(f"  places {x[0]}->{x[1]} true cut {x[3]:.2f}s az {x[4]} parts az {x[5]} cuts "
          + ", ".join(f"{c['by']} {c['t']}s d={c.get('d', c.get('whole'))}" for c in x[2]))
for a in made:
    try:
        urllib.request.urlopen(urllib.request.Request(ADMIN + "/api/delete", data=json.dumps({"audio": a}).encode(),
                                                      headers={"Content-Type": "application/json"}), timeout=10).read()
    except Exception as e:
        print("cleanup", a, e)
print(f"cleaned {len(made)} test clips")
