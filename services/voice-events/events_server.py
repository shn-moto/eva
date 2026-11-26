#!/usr/bin/env python3
"""What a sound is (docs/EMOTION-PLAN.md, stage 2 - log only): PANNs CNN14 (AudioSet, 527
classes) on every phrase the ear sends to STT - speech, a child singing, crying, laughter,
screaming, or not a voice at all (a bird, a car, music, a cupboard door). CPU only: the GPU
is full.

    POST /classify   raw s16le 16 kHz mono (or a WAV)  ->  {"voice", "kinds", "top", "verdict", "ms"}
    GET  /health

voice   - the highest of the voice classes (speech, singing, laughter, crying, screaming...):
          below NOISE_VOICE the phrase is not a voice - the STT made words of a noise
          (2026-11-26: 86 of 367 short phrases, 2 of 711 family phrases at 0.15)
kinds   - the classes the scenarios care about: cry, laugh, scream, shout, singing, music,
          bird, tv
verdict - "noise" | "cry" | "scream" | "laugh" | "speech"
emotion - speech emotion (GigaAM-Emo, Dusha: angry / sad / neutral / positive), only for a voice
          of at least EMO_MIN s: on one short word it is confident nonsense («так» = angry 0.98,
          stage 1 of the plan); {"label", "score", "probs"}

    MODELS   the cache with panns_data/Cnn14_mAP=0.431.pth (~/eva-data/bench-cache)
    THREADS  torch threads (4)
"""
import io
import os
import time
import wave

import numpy as np
import torch
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool

torch.set_num_threads(int(os.environ.get("THREADS", "4")))
from panns_inference import AudioTagging, labels  # noqa: E402

app = FastAPI()
MODEL = AudioTagging(checkpoint_path=None, device="cpu")
L = list(labels)
NOISE_VOICE = float(os.environ.get("NOISE_VOICE", "0.15"))
EMO_MIN = float(os.environ.get("EMO_MIN", "2.0"))           # s of a voice before its emotion is asked
EMO = None
if os.environ.get("EMOTION", "1") == "1":
    try:
        import gigaam
        EMO = gigaam.load_model("emo", device="cpu")   # its checkpoint in the cache volume (gigaam/emo.ckpt)
    except Exception as e:  # the model away: the sound alone
        print("emotion model not loaded:", e, flush=True)

GROUPS = {
    "voice": ["Speech", "Child speech, kid speaking", "Male speech, man speaking", "Female speech, woman speaking",
              "Conversation", "Narration, monologue", "Babbling", "Whispering", "Singing", "Child singing",
              "Female singing", "Male singing", "Humming", "Laughter", "Baby laughter", "Giggle", "Chuckle, chortle",
              "Crying, sobbing", "Baby cry, infant cry", "Whimper", "Screaming", "Shout", "Yell", "Children shouting"],
    "cry": ["Crying, sobbing", "Baby cry, infant cry", "Whimper", "Wail, moan"],
    "laugh": ["Laughter", "Baby laughter", "Giggle", "Chuckle, chortle", "Belly laugh"],
    "scream": ["Screaming"],
    "shout": ["Shout", "Yell", "Children shouting", "Battle cry"],
    "singing": ["Singing", "Child singing", "Female singing", "Male singing"],
    "music": ["Music", "Musical instrument"],
    "bird": ["Bird", "Bird vocalization, bird call, bird song", "Chirp, tweet", "Canary"],
    "tv": ["Television", "Radio"],
}
IDX = {g: [L.index(c) for c in cs if c in L] for g, cs in GROUPS.items()}


def _audio(body):
    """s16le 16 kHz or a WAV -> float32 at 32 kHz (PANNs' rate)."""
    if body[:4] == b"RIFF":
        with wave.open(io.BytesIO(body)) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        if ch > 1:
            a = a.reshape(-1, ch).mean(1)
    else:
        sr, a = 16000, np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768
    n = int(len(a) * 32000 / sr)
    return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)


def classify(body):
    t = time.time()
    a = _audio(body)
    if len(a) < 16000:                    # < 0.5 s
        return {"error": "too short"}
    p, _ = MODEL.inference(a[None, :])
    p = p[0]
    kinds = {g: round(float(max(p[i] for i in ix)), 3) for g, ix in IDX.items() if ix}
    voice = kinds.pop("voice")
    if voice < NOISE_VOICE:
        verdict = "noise"
    elif kinds["cry"] >= 0.3:
        verdict = "cry"
    elif kinds["scream"] >= 0.3:
        verdict = "scream"
    elif kinds["laugh"] >= 0.3:
        verdict = "laugh"
    else:
        verdict = "speech"
    top = sorted(range(len(L)), key=lambda i: -p[i])[:3]
    out = {"voice": round(voice, 3), "kinds": kinds, "verdict": verdict,
           "top": [[L[i], round(float(p[i]), 3)] for i in top]}
    if EMO is not None and voice >= 0.5 and len(a) >= EMO_MIN * 32000:
        out["emotion"] = emotion(a)
    out["ms"] = int(1000 * (time.time() - t))
    return out


def emotion(a32):
    """GigaAM-Emo on a 16 kHz wav (it reads files)."""
    import tempfile
    a16 = a32[::2]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(a16, -1, 1) * 32767).astype("<i2").tobytes())
        with torch.no_grad():
            p = EMO.get_probs(path)
        top = max(p, key=p.get)
        return {"label": top, "score": round(float(p[top]), 3), "probs": {k: round(float(v), 3) for k, v in p.items()}}
    except Exception as e:
        return {"error": str(e)[:200]}
    finally:
        os.remove(path)


@app.post("/classify")
async def classify_route(request: Request):
    return await run_in_threadpool(classify, await request.body())


@app.get("/health")
def health():
    return {"ok": True, "model": "PANNs CNN14", "emotion": EMO is not None, "noise_voice": NOISE_VOICE}
