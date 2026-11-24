#!/usr/bin/env python3
"""LAN STT service for the Station-1 assistant.

POST /stt  body: raw 16-bit PCM (mono 16 kHz) or a WAV file -> {"text": ...}
POST /doa  body: the Station's raw mics (int16, X-Channels) -> the direction
GET  /health -> {"ok": true, "model": ...}

A phrase's /doa and /stt carry the same X-Phrase id: /stt then splits the
phrase by the direction too and keeps the direction with the clip.
"""
import asyncio
import io
import json
import os
import threading
import time
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


# --- the direction as an angle, and a change of direction inside a phrase (2026-11-24)
# The array's geometry was fitted from the 2026-11-23 test set (tools/split-bench:
# doa_geometry.py, doa_hexagon.py): 6 mics on a circle of 3.65 cm + one in the
# centre. With it every 0.5 s block gets an angle (SRP over the GCC-PHAT curves);
# the room's orientation (where "in front" is) is set on the admin page.
# A change of place inside a phrase: the GCC curves of all 21 pairs left vs right
# of a cut (1 - mean cosine). On glued test phrases (one voice, 5 places):
# >= 0.25 found 81 % of the changes with 5 % false cuts, >= 0.3 79 % / 1.7 %; the
# cut lands 0.34 s (median) from the true one (tools/split-bench/doa_split.py) and
# is moved to the nearest word start. End to end (split_live.py) at 0.25: 80 % / 10 %.
GEOMETRY = os.environ.get("DOA_GEOMETRY", os.path.join(os.path.dirname(os.path.abspath(__file__)), "doa_geometry.json"))
DIR_CUT = float(os.environ.get("DIR_CUT", "0.3"))
SNAP = 1.2                                    # s: the cut moves to the longest word pause this close
DIR_SIDE = 1.0                                # s of each side of a direction cut
_geo = None


def geometry():
    global _geo
    if _geo is None:
        try:
            g = json.load(open(GEOMETRY))
            _geo = {"pairs": [tuple(x) for x in g["pairs"]], "B": np.asarray(g["baselines"], dtype=np.float64)}
        except (OSError, ValueError, KeyError):
            _geo = {}
    return _geo or None


def frames(m):
    """voiced frames of the raw mics: (X rfft frames x bins x mics, centre times s, pairs)"""
    nmic = m.shape[1]
    pairs = [(i, j) for i in range(nmic) for j in range(i + 1, nmic)]
    if len(m) < DOA_N:
        return None
    win = np.hanning(DOA_N)[:, None]
    starts = np.arange(0, len(m) - DOA_N + 1, DOA_HOP)
    F = np.stack([m[s:s + DOA_N] * win for s in starts])
    energy = (F ** 2).sum((1, 2))
    voiced = energy >= np.percentile(energy, 50)
    X = np.fft.rfft(F, axis=1)
    X[:, :int(200 * DOA_N / 16000)] = 0
    return X[voiced], (starts[voiced] + DOA_N / 2) / 16000, pairs


def _curves(X, pairs, unit=True):
    """GCC-PHAT curve of every pair (pairs x lags, lag k at index k*UP + MAXLAG*UP)."""
    n = (X.shape[1] - 1) * 2
    m = DOA_MAXLAG * DOA_UP
    out = []
    for i, j in pairs:
        c = (X[:, :, i] * np.conj(X[:, :, j])).sum(0)
        c = c / (np.abs(c) + 1e-12)
        r = np.fft.irfft(c, n * DOA_UP)
        r = np.concatenate([r[-m:], r[:m + 1]])
        if unit:
            r = r - r.mean()
            r = r / (np.linalg.norm(r) + 1e-12)
        out.append(r)
    return np.stack(out)


_AZ = np.radians(np.arange(0, 360, 2))


def angle(X, pairs):
    """-> (degrees, strength) of the voice in these frames, or None without a geometry."""
    g = geometry()
    if g is None or len(X) < 2 or len(pairs) != len(g["pairs"]):
        return None
    R = _curves(X, pairs, unit=False)                          # pairs x lags
    m = DOA_MAXLAG * DOA_UP
    lag = g["B"] @ np.stack([np.cos(_AZ), np.sin(_AZ)])         # pairs x angles, in samples
    idx = np.clip(lag * DOA_UP + m, 0, 2 * m)
    lo = np.floor(idx).astype(int)
    hi = np.minimum(lo + 1, 2 * m)
    f = idx - lo
    rows = np.arange(len(pairs))[:, None]
    score = ((1 - f) * R[rows, lo] + f * R[rows, hi]).mean(0)
    k = int(np.argmax(score))
    return round(float(np.degrees(_AZ[k])), 1), round(float(score[k]), 3)


def dir_cut(fr, t0, t1):
    """The cut in [t0, t1) where the place changes most: (distance, t) or (0, None)."""
    X, t, pairs = fr
    scores = []
    c = t0 + DIR_SIDE
    while c <= t1 - DIR_SIDE:
        L, R = X[(t >= t0) & (t < c)], X[(t >= c) & (t < t1)]
        if len(L) >= 8 and len(R) >= 8:
            scores.append((c, 1.0 - float(np.mean((_curves(L, pairs) * _curves(R, pairs)).sum(1)))))
        c += 0.25
    if not scores:
        return (0.0, None)
    k = max(range(len(scores)), key=lambda i: scores[i][1])
    best = scores[k][1]
    # the pause between two people is unvoiced: every cut inside it scores the
    # same - the middle of that plateau, not its first point (the first was a
    # word early on 11 of 16 test glues)
    lo = hi = k
    while lo > 0 and scores[lo - 1][1] >= 0.95 * best:
        lo -= 1
    while hi < len(scores) - 1 and scores[hi + 1][1] >= 0.95 * best:
        hi += 1
    return (round(best, 3), round((scores[lo][0] + scores[hi][0]) / 2, 2))


def dir_summary(fr, length_s):
    """For the clip's json and the admin's radar: the phrase's angle and one per 0.5 s."""
    X, t, pairs = fr
    whole = angle(X, pairs)
    blocks = []
    b = 0.0
    while b < length_s:
        sel = (t >= b) & (t < b + DOA_BLOCK)
        a = angle(X[sel], pairs) if sel.sum() >= 2 else None
        blocks.append([round(b, 2)] + (list(a) if a else [None, None]))
        b += DOA_BLOCK
    return {"az": whole[0] if whole else None, "strength": whole[1] if whole else None, "blocks": blocks}


# the raw mics of a phrase wait here for its /stt (same X-Phrase id)
_dir = {}
_dir_lock = threading.Lock()


def _dir_put(pid, raw):
    with _dir_lock:
        now = time.time()
        for k in [k for k, v in _dir.items() if now - v["at"] > 60]:
            del _dir[k]
        _dir[pid] = {"at": now, "raw": raw}


async def _dir_get(pid, wait=1.5):
    end = time.time() + wait
    while True:
        with _dir_lock:
            hit = _dir.pop(pid, None)
        if hit or time.time() >= end:
            return hit
        await asyncio.sleep(0.05)


@app.post("/doa")
async def doa_route(request: Request):
    """body: int16 interleaved, X-Channels channels, 16 kHz (the Station's raw mics)"""
    ch = int(request.headers.get("X-Channels", "7"))
    body = await request.body()
    raw = np.frombuffer(body, dtype=np.int16).reshape(-1, ch)
    if request.headers.get("X-Keep") and LOG_DIR:
        _keep_raw(raw)
    pid = request.headers.get("X-Phrase")
    if pid:
        _dir_put(pid, raw)
    m = raw.astype(np.float32) / 32768.0
    m = m - m.mean(0)
    out = doa(m) or {}
    fr = frames(m) if out else None
    if fr is not None and len(fr[0]) >= 2:
        a = angle(fr[0], fr[2])
        if a:
            out["az"], out["az_strength"] = a
    return out


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
LOG_DAYS = int(os.environ.get("STT_LOG_DAYS", "30"))


RAW_DAYS = int(os.environ.get("STT_RAW_DAYS", "7"))   # the 7 raw mics of every phrase: re-run new splitters on them


def keep(audio, out, ms, raw=None):
    """The phrase as the model got it + what it heard: real home audio (music,
    children, two voices at once) for the next STT and front-end tests - the
    2026-11-19 test set was recorded in a quiet room (2026-11-23).
    -> its name "<date>/<time>" (the brain stores it with the phrase in the
    memory, the admin page plays it), None if not kept."""
    if not LOG_DIR:
        return None
    import shutil
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
        if info.get("parts"):
            info["parts"] = [{k: v for k, v in x.items() if k != "emb"} for x in info["parts"]]
        with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        if raw is not None:
            # "-mics": not "-raw7", the test sets' recordings, which are kept
            with wave.open(os.path.join(d, name + "-mics.wav"), "wb") as w:
                w.setnchannels(raw.shape[1])
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(raw.astype("<i2").tobytes())
        # older days go; a phrase marked to keep on the admin page was copied
        # out to the keep folder before that
        days = sorted(os.listdir(LOG_DIR))
        for old in days[:-LOG_DAYS]:
            shutil.rmtree(os.path.join(LOG_DIR, old), ignore_errors=True)
        for old in days[-LOG_DAYS:-RAW_DAYS]:                  # the raw mics go sooner
            for fn in os.listdir(os.path.join(LOG_DIR, old)):
                if fn.endswith("-mics.wav"):
                    os.remove(os.path.join(LOG_DIR, old, fn))
        return os.path.basename(d) + "/" + name
    except OSError as e:
        print("keep:", e, flush=True)
        return None


@app.post("/stt")
async def stt(request: Request):
    t = time.time()
    body = await request.body()
    if len(body) < 3200:  # < 0.1c звука
        return JSONResponse({"text": ""})
    audio = _pcm_from_request(body)
    if len(audio) < 1600:
        return {"text": ""}
    split = request.query_params.get("split") == "1"
    pid = request.headers.get("X-Phrase")
    if not ENGINE.startswith("gigaam"):
        out = _stt(audio, split)
        raw = None
    else:
        text, letters = decode(audio)
        hit = await _dir_get(pid) if pid else None
        raw = hit["raw"] if hit else None
        out = {"text": text, "duration": round(len(audio) / 16000, 2)}
        fr = None
        if raw is not None:
            m = raw.astype(np.float32) / 32768.0
            m = m - m.mean(0)
            if abs(len(m) - len(audio)) <= 0.2 * 16000:       # the mics and the mono are one stretch of sound
                fr = frames(m)
            if fr is not None and len(fr[0]) >= 2:
                out["doa"] = dir_summary(fr, len(audio) / 16000)
        if split:
            parts = split_phrase(audio, fr, letters)
            if len(parts) > 1:
                out["parts"] = parts
    name = keep(audio, dict(out, record=True) if request.headers.get("X-Record") else out,
                int(1000 * (time.time() - t)), raw)
    if name:
        out = dict(out, audio=name)
    if out.get("doa"):
        out["doa"] = {k: v for k, v in out["doa"].items() if k != "blocks"}   # the ear needs the angle only
    return out


def decode(audio):
    """The whole phrase once: its text and its letters with their times."""
    rec = get_recognizer()
    s = rec.create_stream()
    s.accept_waveform(16000, audio)
    rec.decode_streams([s])
    r = s.result
    return r.text.strip(), list(zip(r.tokens, r.timestamps))


def _text_between(letters, t0, t1):
    """The words that start in [t0, t1): a word belongs to the side its first letter is on."""
    words, cur, start = [], "", None
    for ch, ts in letters + [(" ", 1e9)]:
        if ch.isspace():
            if cur:
                words.append((start, cur))
            cur, start = "", None
        else:
            if not cur:
                start = ts
            cur += ch
    return " ".join(w for st, w in words if t0 <= st < t1)


MAX_PARTS = 3


def split_phrase(audio, fr, letters):
    """Where another person took over inside the phrase: by the direction (a
    change of place, any voice) and by the voice (a change of voice, the same
    place). Up to MAX_PARTS parts; their text is taken from the whole's letters,
    not recognised again (a half recognised alone came out worse: "жмелец" for
    "жмёт. палец", 2026-11-24)."""
    sr = 16000
    end = len(audio) / sr
    cuts = []

    def search(t0, t1):
        if len(cuts) >= MAX_PARTS - 1 or t1 - t0 < 2 * min(DIR_SIDE, PART_MIN):
            return
        cut = None
        if fr is not None:
            d, c = dir_cut(fr, t0, t1)
            if c is not None and d >= DIR_CUT:
                cut = {"t": c, "by": "direction", "d": d}
        if cut is None and t1 - t0 >= SPLIT_MIN:
            ch = speaker_change(audio[int(t0 * sr):int(t1 * sr)])
            if ch:
                cut = {"t": round(t0 + ch["t"], 2), "by": "voice", "win": ch["win"], "whole": ch["whole"]}
        if cut is None:
            return
        cuts.append(cut)
        search(t0, cut["t"])
        search(cut["t"], t1)

    search(0.0, end)
    if not cuts:
        return []
    # to the longest pause between two words near the cut: where one person
    # stopped and the other began. The nearest word start was not it - it jumped
    # into the first person's sentence (0.8 s early on 2 of 12 test glues). The
    # letters' times are when the model emits them, a little after the sound.
    words = []                                                # (first letter, last letter) times
    for k, (ch, ts) in enumerate(letters):
        if ch.isspace():
            continue
        if k == 0 or letters[k - 1][0].isspace():
            words.append([ts, ts])
        else:
            words[-1][1] = ts
    for c in cuts:
        gaps = [(words[k + 1][0] - words[k][1], k) for k in range(len(words) - 1)
                if abs(words[k + 1][0] - c["t"]) <= SNAP]
        if gaps:
            g, k = max(gaps)
            nxt = words[k + 1][0]
            c["raw_t"], c["t"] = c["t"], round(min(max(words[k][1] + 0.05, nxt - 0.25), nxt - 0.01), 2)
    cuts.sort(key=lambda c: c["t"])
    cuts = [c for k, c in enumerate(cuts) if k == 0 or c["t"] - cuts[k - 1]["t"] >= 0.5]
    bounds = [0.0] + [c["t"] for c in cuts] + [end]
    parts = []
    for k in range(len(bounds) - 1):
        a, b = bounds[k], bounds[k + 1]
        seg = audio[int(a * sr):int(b * sr)]
        p = {"from": round(a, 2), "to": round(b, 2), "text": _text_between(letters, a, b),
             "emb": spk_emb(seg).round(5).tolist()}
        if k > 0:
            p["cut"] = cuts[k - 1]
        if fr is not None:
            X, t, pairs = fr
            sel = (t >= a) & (t < b)
            if sel.sum() >= 2:
                p["lags"] = _gcc(X[sel], pairs)[0]
                az = angle(X[sel], pairs)
                if az:
                    p["az"] = az[0]
        parts.append(p)
    return parts


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
