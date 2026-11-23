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

# whisper | gigaam (CTC) | gigaam-rnnt (transducer: GigaAM v3 RNNT won the
# 2026-11-19 test, new_alisa/docs/stt-test; Whisper is ~22 s/phrase on this
# AVX-only Xeon)
ENGINE = os.environ.get("STT_ENGINE", "whisper")
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
    """GigaAM (sherpa-onnx), Russian: CTC or RNNT by STT_ENGINE."""
    global _rec
    if _rec is None:
        import sherpa_onnx
        if ENGINE == "gigaam-rnnt":
            _rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=f"{GIGA_DIR}/encoder.int8.onnx", decoder=f"{GIGA_DIR}/decoder.onnx",
                joiner=f"{GIGA_DIR}/joiner.onnx", tokens=f"{GIGA_DIR}/tokens.txt",
                model_type="nemo_transducer", num_threads=THREADS)
            return _rec
        _rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=f"{GIGA_DIR}/model.int8.onnx",
            tokens=f"{GIGA_DIR}/tokens.txt",
            num_threads=THREADS)
    return _rec


def transcribe(audio: np.ndarray) -> tuple[str, float]:
    if ENGINE.startswith("gigaam"):
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


# --- a change of speaker inside one phrase (two people talking without a pause)
# CAM++ (the same model as on the Station, so voices compare with its
# profiles). Calibrated 2026-11-19 on the Station's recordings
# (new_alisa/docs/stt-test): sliding windows find the likeliest boundary, the
# two whole parts confirm it. Windows < 0.5 and parts < 0.45 is offered; the
# Station decides (parts < 0.35, or two different family voices).
SPK_MODEL = os.environ.get("SPK_MODEL", "/models/spk/campplus.onnx")
SPLIT_MIN = 3.0                   # s: shorter phrases are one speaker
WIN, HOP = 1.5, 0.5
PART_MIN = 1.2                    # s of each part
WIN_MAX, WHOLE_MAX = 0.5, 0.45
_spk = None


def spk_emb(a):
    global _spk
    if _spk is None:
        import sherpa_onnx
        _spk = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=SPK_MODEL, num_threads=4))
    a = a / (np.abs(a).max() + 1e-9) * 0.9
    s = _spk.create_stream()
    s.accept_waveform(16000, a.astype(np.float32))
    s.input_finished()
    e = np.array(_spk.compute(s), dtype=np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


def speaker_change(audio):
    """-> {"t", "win", "whole", "embs": [left, right]} or None"""
    sr = 16000
    if len(audio) < SPLIT_MIN * sr:
        return None
    n, h = int(WIN * sr), int(HOP * sr)
    W = [spk_emb(audio[i:i + n]) for i in range(0, len(audio) - n + 1, h)]
    best = (1.0, None)
    for k in range(1, len(W)):
        L, R = np.mean(W[:k], 0), np.mean(W[k:], 0)
        s = float(L @ R / (np.linalg.norm(L) * np.linalg.norm(R)))
        if s < best[0]:
            best = (s, k * HOP + WIN / 2)
    win, t = best
    if t is None or win >= WIN_MAX:
        return None
    cut = int(t * sr)
    if cut < PART_MIN * sr or len(audio) - cut < PART_MIN * sr:
        return None
    left, right = spk_emb(audio[:cut]), spk_emb(audio[cut:])
    whole = float(left @ right)
    if whole >= WHOLE_MAX:
        return None
    return {"t": round(t, 2), "win": round(win, 3), "whole": round(whole, 3),
            "embs": [left.round(5).tolist(), right.round(5).tolist()]}


# Direction of a voice from the Station's 7 microphones (2026-11-23: all 7
# found in the capture - before only 3 were used). GCC-PHAT time differences
# for all 21 pairs = a fingerprint of the direction: the array geometry is not
# needed to tell "same place" from "another place". Blocks of 0.5 s show a
# change of direction inside a phrase (someone else took over).
DOA_N, DOA_HOP, DOA_UP = 512, 256, 8         # frame, hop, cross-correlation upsampling
DOA_MAXLAG = 6                               # samples at 16 kHz: ~13 cm, more than the array spans
DOA_BLOCK = 0.5


def _gcc(X, pairs):
    """X: frames x bins x mics (rfft). -> lag per pair (samples, float), peak strength per pair"""
    n = (X.shape[1] - 1) * 2
    lags, peaks = [], []
    for i, j in pairs:
        c = (X[:, :, i] * np.conj(X[:, :, j])).sum(0)
        c = c / (np.abs(c) + 1e-12)
        r = np.fft.irfft(c, n * DOA_UP)
        m = DOA_MAXLAG * DOA_UP
        r = np.concatenate([r[-m:], r[:m + 1]])
        k = int(np.argmax(r))
        lags.append(round((k - m) / DOA_UP, 3))
        peaks.append(round(float(r[k]), 4))
    return lags, peaks


def doa(m):
    """m: samples x 7 float. -> {"lags": [21], "peak": mean peak, "blocks": [[21], ...]}"""
    nmic = m.shape[1]
    pairs = [(i, j) for i in range(nmic) for j in range(i + 1, nmic)]
    win = np.hanning(DOA_N)[:, None]
    fr = [m[s:s + DOA_N] * win for s in range(0, len(m) - DOA_N + 1, DOA_HOP)]
    if not fr:
        return None
    F = np.stack(fr)                                         # frames x N x mics
    energy = (F ** 2).sum((1, 2))
    voiced = energy >= np.percentile(energy, 50)             # the louder half: the voice, not the room
    X = np.fft.rfft(F, axis=1)
    X[:, :int(200 * DOA_N / 16000)] = 0                      # below 200 Hz: hum and rumble
    lags, peaks = _gcc(X[voiced], pairs)
    per = max(1, int(DOA_BLOCK * 16000 / DOA_HOP))
    blocks = []
    for b in range(0, len(F), per):
        sel = voiced[b:b + per]
        if sel.sum() >= 2:
            blocks.append(_gcc(X[b:b + per][sel], pairs)[0])
        else:
            blocks.append(None)
    return {"lags": lags, "peak": round(float(np.mean(peaks)), 4), "blocks": blocks}


@app.post("/doa")
async def doa_route(request: Request):
    """body: int16 interleaved, X-Channels channels, 16 kHz (the Station's raw mics)"""
    ch = int(request.headers.get("X-Channels", "7"))
    body = await request.body()
    raw = np.frombuffer(body, dtype=np.int16).reshape(-1, ch)
    if request.headers.get("X-Keep") and LOG_DIR:
        _keep_raw(raw)
    m = raw.astype(np.float32) / 32768.0
    m = m - m.mean(0)
    return doa(m) or {}


def _keep_raw(raw):
    """The Station's raw mics of a test-set phrase (7 channels, 16 kHz), next to
    its mono phrase in the STT log: for front-end work (first word, direction)."""
    import time
    d = os.path.join(LOG_DIR, time.strftime("%Y-%m-%d"))
    try:
        os.makedirs(d, exist_ok=True)
        with wave.open(os.path.join(d, time.strftime("%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-raw{raw.shape[1]}.wav"), "wb") as w:
            w.setnchannels(raw.shape[1])
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(raw.astype("<i2").tobytes())
    except OSError as e:
        print("keep raw:", e, flush=True)


@app.get("/health")
def health():
    return {"ok": True, "engine": ENGINE, "model": GIGA_DIR if ENGINE.startswith("gigaam") else MODEL_NAME}


LOG_DIR = os.environ.get("STT_LOG_DIR", "")      # every phrase kept here (shn-linux), not on the Station
LOG_DAYS = int(os.environ.get("STT_LOG_DAYS", "14"))


def keep(audio, out, ms):
    """The phrase as the model got it + what it heard: real home audio (music,
    children, two voices at once) for the next STT and front-end tests - the
    2026-11-19 test set was recorded in a quiet room (2026-11-23)."""
    if not LOG_DIR:
        return
    import json
    import shutil
    import time
    d = os.path.join(LOG_DIR, time.strftime("%Y-%m-%d"))
    try:
        os.makedirs(d, exist_ok=True)
        name = time.strftime("%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        pcm = np.clip(audio * 32767, -32767, 32767).astype(np.int16)
        with wave.open(os.path.join(d, name + ".wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())
        info = {k: v for k, v in out.items() if k != "split"}
        if out.get("split"):
            info["split"] = {k: v for k, v in out["split"].items() if k != "embs"}
        info["ms"] = ms
        with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        for old in sorted(os.listdir(LOG_DIR))[:-LOG_DAYS]:
            shutil.rmtree(os.path.join(LOG_DIR, old), ignore_errors=True)
    except OSError as e:
        print("keep:", e, flush=True)


@app.post("/stt")
async def stt(request: Request):
    import time
    t = time.time()
    body = await request.body()
    if len(body) < 3200:  # < 0.1c звука
        return JSONResponse({"text": ""})
    audio = _pcm_from_request(body)
    if len(audio) < 1600:
        return {"text": ""}
    out = _stt(audio, request.query_params.get("split") == "1")
    keep(audio, dict(out, record=True) if request.headers.get("X-Record") else out, int(1000 * (time.time() - t)))
    return out


def _stt(audio, split):
    if split:
        ch = speaker_change(audio)
        if ch and ENGINE.startswith("gigaam"):
            # the whole and both parts in one batch: the Station may keep the
            # phrase whole, and then it needs the whole's text (a cut in one
            # voice's phrase spoils the words at the cut)
            cut = int(ch["t"] * 16000)
            rec = get_recognizer()
            streams = []
            for part in (audio, audio[:cut], audio[cut:]):
                s = rec.create_stream()
                s.accept_waveform(16000, part)
                streams.append(s)
            rec.decode_streams(streams)
            whole, *ch["texts"] = [s.result.text.strip() for s in streams]
            return {"text": whole, "duration": round(len(audio) / 16000, 2), "split": ch}
    text, dur = transcribe(audio)
    return {"text": text, "duration": round(dur, 2)}


@app.on_event("startup")
def warmup():
    if ENGINE.startswith("gigaam"):
        get_recognizer()
    else:
        get_model()
