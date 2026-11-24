#!/usr/bin/env python3
"""Recompute the direction kept with the clips from their raw mics (-mics.wav),
after a change of the direction code (the band, the geometry).

    docker exec -i -w /app newalisa-stt python - < redo_doa.py  [DAY_DIR]

Only the json's "doa" changes; the parts (the split) stay as they were made.
"""
import glob
import json
import os
import sys
import wave

import numpy as np

import stt_server as S

DAY = sys.argv[1] if len(sys.argv) > 1 else "/log/" + __import__("time").strftime("%Y-%m-%d")
n = 0
for mics in sorted(glob.glob(os.path.join(DAY, "*-mics.wav"))):
    js = mics[:-len("-mics.wav")] + ".json"
    if not os.path.exists(js):
        continue
    with wave.open(mics) as w:
        m = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())
    m = m.astype(np.float32) / 32768
    m = m - m.mean(0)
    fr = S.frames(m)
    if fr is None or len(fr[0]) < 2:
        continue
    info = json.load(open(js, encoding="utf-8"))
    old = (info.get("doa") or {}).get("az")
    info["doa"] = S.dir_summary(fr, info.get("duration") or len(m) / 16000)
    with open(js + ".tmp", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    os.replace(js + ".tmp", js)
    n += 1
    if os.path.basename(js).startswith("1329"):
        print(os.path.basename(js), old, "->", info["doa"]["az"], [b[1] for b in info["doa"]["blocks"]])
print(f"redone {n} clips in {DAY}")
