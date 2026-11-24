#!/usr/bin/env python3
"""What a 7-mic beam gives over the ear's mean of 3 mics, on recorded phrases
(their raw mics, -mics.wav / -raw7.wav). SNR proxy: the loudest 20 % of 32 ms
frames (the words) over the quietest 20 % (the room), in the speech band.

    docker exec -i -w /app newalisa-stt python - < beam_gain.py  FILE ...
"""
import json
import sys
import wave

import numpy as np

import stt_server as S

SR = 16000
HEAR = [0, 2, 4]
G = json.load(open(S.GEOMETRY))
Q = np.asarray(G["mics"], dtype=np.float64)          # mic positions, samples at 16 kHz


def load(p):
    with wave.open(p) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())
    m = a.astype(np.float64) / 32768
    return m - m.mean(0)


def band(x, lo=200, hi=3500):
    X = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, len(x), axis=0)


def delay(x, d):
    """x delayed by d samples (fractional), by the FFT."""
    n = len(x)
    f = np.fft.rfftfreq(n)
    return np.fft.irfft(np.fft.rfft(x) * np.exp(-2j * np.pi * f * d), n)


def beam(m, az, sign):
    u = np.array([np.cos(np.radians(az)), np.sin(np.radians(az))])
    d = sign * (Q @ u)                                # each mic's lead (samples)
    d = d - d.min()
    return np.mean([delay(m[:, k], d[k]) for k in range(m.shape[1])], 0)


def snr(x):
    fr = x[: len(x) // 512 * 512].reshape(-1, 512)
    e = np.sort((fr ** 2).mean(1))
    k = max(1, len(e) // 5)
    return 10 * np.log10(e[-k:].mean() / (e[:k].mean() + 1e-15))


for p in sys.argv[1:]:
    m = band(load(p))
    fr = S.frames(load(p))
    az = S.angle(fr[0], fr[2])[0] if fr is not None and len(fr[0]) >= 2 else 0.0
    out = {"mean3": snr(m[:, HEAR].mean(1)), "mean7": snr(m.mean(1))}
    for sign in (1, -1):
        out[f"beam7{'+' if sign > 0 else '-'}"] = snr(beam(m, az, sign))
        out[f"off{'+' if sign > 0 else '-'}"] = snr(beam(m, (az + 180) % 360, sign))
    base = out["mean3"]
    print(f"{p.split('/')[-1]:28s} az {az:5.0f}  mean3 {base:5.1f} dB  " +
          "  ".join(f"{k} {v - base:+.1f}" for k, v in out.items() if k != "mean3"))
