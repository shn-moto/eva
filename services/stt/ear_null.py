"""A spatial null toward the muted places (the TV), from the 7 raw microphones (user, 2026-11-26).

The places muted on the admin page («Места», mode "mute") are not only dropped by logic
(hear.muted_place): their sound is taken out of what the ear listens to, before the echo
canceller, the VAD and the STT - so the TV no longer makes phrases, and a person talking over it
is heard cleaner.

Data, not geometry: the STT log keeps every phrase's raw 7 mics (-mics.wav) and its direction per
0.5 s. From the blocks that point at a muted place we take the covariance of its sound per
frequency (R_tv), from family phrases from elsewhere the covariance of the speech to keep (R_s).
Per frequency the weights maximise speech over the TV (the generalised eigenvector of R_s and
R_tv + loading), scaled so that the speech comes out as the old mono (the mean of HEAR_MICS) did.
The capsules' polarity, the room's reflections and the array's real geometry are in the data.

Measured 2026-11-26 on held-out clips (16 TV clips, 195 family phrases): the TV -13 dB (worst -9),
speech from elsewhere -2 dB, with loading 0.1. Lower loading nulls deeper (-18 dB) but eats voices.

Below ~150-500 Hz the array is too small to tell directions: the weights fade to the plain mono.
The filter is linear and fixed, so the echo canceller after it still sees one echo path.
"""
import glob
import json
import os
import threading
import time
import wave

import numpy as np

N, HOP, SR = 512, 256, 16000
WIN = np.sqrt(np.hanning(N + 1)[:N])          # sqrt-Hann: analysis x synthesis sums to 1 at 50 %
F = np.fft.rfftfreq(N, 1 / SR)
HEAR = [0, 2, 4]                              # the old mono: the mean of these (ear_stream.HEAR_MICS)
W0 = np.zeros(7)
W0[HEAR] = 1 / 3
LOG_DIR = os.environ.get("STT_LOG_DIR", "/log")
NULL_FILE = os.path.join(LOG_DIR, "ear-null.npz")
EPS = float(os.environ.get("EAR_NULL_EPS", "0.1"))
TOL = 12.0                                    # degrees: a block points at the place
DAYS = 14                                     # how far back clips are taken from
MIN_FRAMES = 1500                             # ~24 s of the place's sound at least

STATE = {"null": None}


def log(*a):
    print(time.strftime("%H:%M:%S"), "null:", *a, flush=True)


def adiff(a, b):
    return abs((a - b + 540) % 360 - 180)


class Null:
    """Streaming STFT filter: 7 mics in (n x 7, int16 scale), mono out, 256 samples late."""

    def __init__(self):
        self.W = np.tile(W0.astype(complex), (len(F), 1))
        self.info = None
        self.prev = np.zeros((HOP, 7))
        self.acc = np.zeros(N)
        self.pend = np.zeros((0, 7))

    def load(self, places):
        """The saved weights, if they were made for these places. -> True if loaded."""
        try:
            d = np.load(NULL_FILE, allow_pickle=False)
            W, info = d["W"], json.loads(str(d["info"]))
        except (OSError, KeyError, ValueError):
            return False
        if W.shape != self.W.shape or info.get("places") != [n for n, _ in places] \
                or info.get("az") != [round(a) for _, a in places]:
            return False
        self.W, self.info = W, info
        log(f"weights loaded: {info}")
        return True

    def set(self, W, info):
        self.W, self.info = W, info

    def process(self, mics):
        self.pend = np.concatenate([self.pend, mics])
        out = []
        W = self.W
        while len(self.pend) >= HOP:
            cur, self.pend = self.pend[:HOP], self.pend[HOP:]
            X = np.fft.rfft(np.concatenate([self.prev, cur]) * WIN[:, None], axis=0)    # bins x 7
            self.prev = cur
            y = np.fft.irfft((W.conj() * X).sum(axis=1), N) * WIN
            self.acc += y
            out.append(self.acc[:HOP].copy())
            self.acc = np.concatenate([self.acc[HOP:], np.zeros(HOP)])
        return np.concatenate(out) if out else np.zeros(0)


# ---- calibration from the STT log ----

def _mics(base):
    w = wave.open(base + "-mics.wav")
    return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, w.getnchannels()).astype(np.float64) / 32768


def _stft(x):
    n = (len(x) - N) // HOP + 1
    if n <= 0:
        return np.zeros((0, len(F), 7), complex)
    idx = np.arange(N)[None, :] + HOP * np.arange(n)[:, None]
    return np.fft.rfft(x[idx] * WIN[None, :, None], axis=1)


def _clips():
    days = sorted(d for d in os.listdir(LOG_DIR) if len(d) == 10 and d[4] == "-")[-DAYS:]
    for day in days:
        for f in sorted(glob.glob(os.path.join(LOG_DIR, day, "*.json"))):
            if "-raw" in f or not os.path.exists(f[:-5] + "-mics.wav"):
                continue
            try:
                d = json.load(open(f, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            doa = d.get("doa") or {}
            if doa.get("az") is None or d.get("echo"):
                continue
            yield f[:-5], d, doa


def _place_frames(base, doa, azs):
    X = _stft(_mics(base))
    keep = np.zeros(len(X), bool)
    blocks = doa.get("blocks") or []
    for t, az, _ in blocks:
        if az is not None and min(adiff(az, a) for a in azs) <= TOL:
            keep[int(t * SR / HOP):int((t + 0.5) * SR / HOP)] = True
    if not any(az is not None for _, az, _ in blocks):
        keep[:] = True
    return X[keep]


def _weights(Rt, Rs, eps=EPS, lo=150.0, hi=500.0):
    alpha = np.clip((F - lo) / (hi - lo), 0, 1)
    W = np.tile(W0.astype(complex), (len(F), 1))
    for k in range(len(F)):
        B = Rt[k] + eps * np.trace(Rt[k]).real / 7 * np.eye(7)
        Li = np.linalg.inv(np.linalg.cholesky(B))
        _, U = np.linalg.eigh(Li @ Rs[k] @ Li.conj().T)
        v = Li.conj().T @ U[:, -1]
        g = (v.conj() @ Rs[k] @ W0) / (v.conj() @ Rs[k] @ v).real
        W[k] = (1 - alpha[k]) * W0 + alpha[k] * v * np.conj(g)
    return W


def _band_db(X, W, f0=300, f1=4000):
    sel = (F >= f0) & (F <= f1)
    Y = np.einsum("tfi,fi->tf", X, W.conj())
    Y0 = np.einsum("tfi,i->tf", X, W0)
    return float(10 * np.log10((np.abs(Y[:, sel]) ** 2).sum() / ((np.abs(Y0[:, sel]) ** 2).sum() + 1e-20) + 1e-20))


def calibrate(places, family):
    """places: [(name, az)] muted; family: the voice base's known names (their phrases from
    elsewhere are the speech to keep). -> (W, info) or (None, reason)."""
    azs = [a for _, a in places]
    tv, fam = [], []
    for base, d, doa in _clips():
        if adiff(doa["az"], min(azs, key=lambda a: adiff(doa["az"], a))) <= TOL and (d.get("duration") or 0) > 2:
            tv.append((base, doa))
        elif min(adiff(doa["az"], a) for a in azs) > 60 and (d.get("duration") or 0) > 1.2 \
                and any((w.get("label") or "") in family for w in d.get("who") or []):
            fam.append(base)
    tv, fam = tv[-80:], fam[-300:]
    if len(tv) < 4 or len(fam) < 20:
        return None, f"too few clips: {len(tv)} from the place, {len(fam)} family phrases elsewhere"
    train, test = tv[::2], tv[1::2]
    Rt = np.zeros((len(F), 7, 7), complex)
    n = 0
    for base, doa in train:
        X = _place_frames(base, doa, azs)
        Rt += np.einsum("tfi,tfj->fij", X, X.conj())
        n += len(X)
    if n < MIN_FRAMES / 2:
        return None, f"too little of the place's sound: {n * HOP / SR:.0f} s"
    Rt /= len(train)
    ftrain, ftest = fam[::2], fam[1::2]
    Rs = np.zeros((len(F), 7, 7), complex)
    for base in ftrain:
        X = _stft(_mics(base))
        Rs += np.einsum("tfi,tfj->fij", X, X.conj())
    Rs /= len(ftrain)
    W = _weights(Rt, Rs)
    tvd = [_band_db(_place_frames(b, doa, azs), W) for b, doa in test]
    fd = [_band_db(_stft(_mics(b)), W) for b in ftest[:80]]
    info = {"places": [p for p, _ in places], "az": [round(a) for a in azs], "made": time.strftime("%Y-%m-%d %H:%M"),
            "clips": len(tv), "family": len(fam), "seconds": round(n * HOP / SR),
            "place_db": round(float(np.median(tvd)), 1), "speech_db": round(float(np.median(fd)), 1), "eps": EPS}
    return W, info


def recalibrate(places, family, wanted=None):
    """In a thread: new weights for the places, saved and put into the running ear - if they are
    still wanted when ready (wanted(): the switch may have gone off meanwhile)."""
    def run():
        try:
            W, info = calibrate(places, family)
        except Exception as e:
            log(f"calibration failed: {e}")
            return
        null = STATE["null"]
        if W is None:
            log(f"not calibrated: {info}")
            return
        np.savez(NULL_FILE + ".tmp.npz", W=W, info=json.dumps(info, ensure_ascii=False))
        os.replace(NULL_FILE + ".tmp.npz", NULL_FILE)
        with open(NULL_FILE[:-4] + ".json", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)          # for the admin page (no numpy there)
        if null is not None and (wanted is None or wanted()):
            null.set(W, info)
        log(f"calibrated: {info}")
    threading.Thread(target=run, daemon=True).start()


def plain():
    """Off, or no place to null: the plain mono again (the saved weights stay for next time)."""
    null = STATE["null"]
    if null is not None:
        null.set(np.tile(W0.astype(complex), (len(F), 1)), None)
