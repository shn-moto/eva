#!/usr/bin/env python3
"""Запись голосового профиля для распознавания говорящего.

Запуск на колонке:
    /opt/assistant/venv/bin/python /opt/assistant/enroll_speaker.py Ivan 3

Скрипт попросит произнести N фраз (по 2-6 секунд каждая, обычной речью),
вычислит CAM++ эмбеддинг каждой и допишет векторы в
/opt/assistant/speakers.json. Ассистент подхватит профиль на лету.
"""
import json
import os
import subprocess
import sys

import numpy as np

REC_DEV = "hw:snddaudio0,0"
REC_RATE = 16000
REC_CH = 5
FRAME = 480
SPK_MODEL = "/opt/assistant/models/campplus.onnx"
PROFILES = "/opt/assistant/speakers.json"


def open_mic():
    return subprocess.Popen(
        ["arecord", "-D", REC_DEV, "-c", str(REC_CH), "-f", "S32_LE",
         "-r", str(REC_RATE), "-t", "raw", "-q"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def read_frame(proc):
    raw = proc.stdout.read(FRAME * REC_CH * 4)
    if len(raw) < FRAME * REC_CH * 4:
        return None
    return np.frombuffer(raw, dtype=np.int32).reshape(-1, REC_CH).mean(axis=1)


def calibrate(proc, seconds=1.0):
    vals = []
    for _ in range(int(seconds / 0.03)):
        f = read_frame(proc)
        if f is not None:
            vals.append(np.sqrt((f.astype(np.float64) ** 2).mean()))
    return float(np.median(vals)) if vals else 1e6


def listen(proc, noise, max_sec=8.0, timeout=20.0):
    """Одна фраза: ждём голос, пишем до паузы 0.9с."""
    thresh = max(noise * 3.0, 4e5)
    buf, sil, spoken, waited = [], 0.0, 0.0, 0.0
    while True:
        f = read_frame(proc)
        if f is None:
            return None
        rms = np.sqrt((f.astype(np.float64) ** 2).mean())
        if not buf:
            waited += 0.03
            if waited > timeout:
                return None
            if rms > thresh:
                buf.append(f)
        else:
            buf.append(f)
            if rms > thresh * 0.7:
                sil = 0.0
            else:
                sil += 0.03
            spoken += 0.03
            if sil >= 0.9 or spoken >= max_sec:
                break
    if spoken < 0.8:
        return None
    mono = np.concatenate(buf)
    peak = np.abs(mono).max() or 1
    return (mono / peak * min(peak / (2 ** 23), 1.0) * 0.9 * 32767).astype(np.int16)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "Ivan"
    n_phrases = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    import sherpa_onnx
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=SPK_MODEL, num_threads=4)
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    proc = open_mic()
    try:
        print("Калибровка шума, секунду тишины...", flush=True)
        noise = calibrate(proc)
        print(f"шум RMS={noise:.0f}", flush=True)
        embs = []
        i = 0
        while i < n_phrases:
            print(f">>> Фраза {i + 1} из {n_phrases}: говорите!", flush=True)
            utt = listen(proc, noise)
            if utt is None:
                print("    не расслышал, ещё раз.", flush=True)
                continue
            audio = utt.astype(np.float32) / 32768.0
            st = ex.create_stream()
            st.accept_waveform(REC_RATE, audio)
            st.input_finished()
            emb = np.array(ex.compute(st), dtype=np.float32)
            emb /= np.linalg.norm(emb) + 1e-9
            embs.append(emb.tolist())
            print(f"    записано ({len(utt) / REC_RATE:.1f}с)", flush=True)
            i += 1
    finally:
        proc.terminate()

    profiles = {}
    if os.path.exists(PROFILES):
        with open(PROFILES, encoding="utf-8") as f:
            profiles = json.load(f)
    profiles.setdefault(name, []).extend(embs)
    with open(PROFILES, "w", encoding="utf-8") as f:
        json.dump(profiles, f)
    print(f"OK: профиль {name!r}: всего {len(profiles[name])} векторов", flush=True)


if __name__ == "__main__":
    main()
