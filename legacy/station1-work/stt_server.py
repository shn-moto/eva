#!/usr/bin/env python3
"""LAN STT service for the Station-1 assistant.

POST /stt  body: raw 16-bit PCM (mono 16 kHz) or a WAV file -> {"text": ...}
GET  /health -> {"ok": true, "model": ...}
"""
import io
import os
import wave

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL_NAME = os.environ.get("STT_MODEL", "small")
COMPUTE = os.environ.get("STT_COMPUTE", "int8")
THREADS = int(os.environ.get("STT_THREADS", "8"))

ENGINE = os.environ.get("STT_ENGINE", "whisper")  # whisper | gigaam
GIGA_DIR = os.environ.get("GIGAAM_DIR", "/models/gigaam")

app = FastAPI()
model = None
_rec = None


def get_model():
    global model
    if model is None:
        from faster_whisper import WhisperModel
        model = WhisperModel(MODEL_NAME, device="cpu", compute_type=COMPUTE,
                             cpu_threads=THREADS)
    return model


def get_recognizer():
    """GigaAM v2 CTC (sherpa-onnx) — русская модель, неавторегрессивная."""
    global _rec
    if _rec is None:
        import sherpa_onnx
        _rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=f"{GIGA_DIR}/model.int8.onnx",
            tokens=f"{GIGA_DIR}/tokens.txt",
            num_threads=THREADS)
    return _rec


def transcribe(audio: np.ndarray) -> tuple[str, float]:
    if ENGINE == "gigaam":
        rec = get_recognizer()
        s = rec.create_stream()
        s.accept_waveform(16000, audio)
        rec.decode_stream(s)
        return s.result.text.strip(), len(audio) / 16000.0
    m = get_model()
    segs, info = m.transcribe(audio, language="ru", beam_size=1,
                              vad_filter=True,
                              condition_on_previous_text=False)
    return " ".join(s.text for s in segs).strip(), info.duration


def _pcm_from_request(body: bytes) -> np.ndarray:
    """WAV (любой) или сырой s16le 16kHz -> float32 mono 16kHz."""
    if body[:4] == b"RIFF":
        with wave.open(io.BytesIO(body), "rb") as w:
            ch, sw, sr, n = (w.getnchannels(), w.getsampwidth(),
                             w.getframerate(), w.getnframes())
            raw = w.readframes(n)
        a = np.frombuffer(raw, dtype=np.int16 if sw == 2 else np.uint8)
        if sw == 1:
            a = (a.astype(np.float32) - 128.0) / 128.0
        else:
            a = a.astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        if sr != 16000:
            idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
            a = np.interp(idx, np.arange(len(a)), a)
        return a.astype(np.float32)
    return np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768.0


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_NAME, "compute": COMPUTE}


@app.post("/stt")
async def stt(request: Request):
    body = await request.body()
    if len(body) < 3200:  # < 0.1c звука
        return JSONResponse({"text": ""})
    audio = _pcm_from_request(body)
    if len(audio) < 1600:
        return {"text": ""}
    text, dur = transcribe(audio)
    return {"text": text, "duration": round(dur, 2)}


@app.on_event("startup")
def warmup():
    if ENGINE == "gigaam":
        get_recognizer()
    else:
        get_model()
