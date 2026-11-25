#!/usr/bin/env python3
"""Eva's hearing on the Station: only the physical part (docs/EAR-PLAN.md).

Capture: 5 channels at 16 kHz from the mic board (snddaudio0):
    7 microphones and the amplifier tap - exactly what the speakers play,
    sampled by the same ADC clock as the mics: the echo canceller's reference.
Echo cancelling: partitioned-block frequency-domain NLMS (overlap-save),
removes Eva's own voice (and music) from the mic signal, so she can be
interrupted while she talks. It needs the reference sample-aligned with the
mics, so it stays here.
Fragments: Silero VAD on the echo-cancelled signal; a fragment ends at a pause
(END_SILENCE) or when it grew too long (SOFT_MAX at the next short dip,
MAX_SPEECH anyway - "chained": the speech goes on). While her voice plays the
allowed length is PLAYBACK_WINDOW, so a «стоп» over her is heard.
Each fragment (mono + the 7 raw mics + its timeline and the canceller's
measures) goes to shn-linux (services/stt/hear.py), which decides everything
else - echo, text, direction, who, where one person ends and another begins,
which fragments are one sentence - and hands the utterances to the brain,
which runs there too (docs/BRAIN-PLAN.md).
"""
import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np
import onnxruntime as ort

from core import profile

SR = 16000
CHUNK = 512                       # 32 ms, Silero's frame
# Capture: 5 words of 32 bits, each = TWO 16-bit TDM slots (the CPU side runs
# 8 x 32-bit slots, the five TLV320ADC3101 16 x 16-bit). Mapped 2026-11-23 by
# muting each ADC channel in turn: word k holds ADC(k-1 mod 5), high half =
# its right channel, low half = its left. Only the high halves of words 1-3
# were used before - 3 of the 7 microphones.
#   word 0: hi ADC4 R tap   lo ADC4 L tap
#   word 1: hi ADC0 R mic   lo ADC0 L mic     (likewise words 2, 3: ADC1, ADC2)
#   word 4: hi ADC3 R tap (speaker amplifier = the canceller's reference)   lo ADC3 L mic
MIC_SLOTS = ((1, "h"), (1, "l"), (2, "h"), (2, "l"), (3, "h"), (3, "l"), (4, "l"))
REF_SLOT = (4, "h")
# indexes into MIC_SLOTS that are mixed for listening: the high halves of words
# 1-3, the three mics that always worked (same polarity)
HEAR_MICS = [0, 2, 4]


def unpack(raw):
    """-> (mics: n x 7, ref: n) as float, full scale 1.0 (as the old S32 / 2^31)."""
    w = np.frombuffer(raw, dtype="<i4").reshape(-1, 5)
    hi = (w >> 16).astype(np.int16)
    lo = (w & 0xFFFF).astype(np.uint16).view(np.int16)
    half = {"h": hi, "l": lo}
    mics = np.stack([half[h][:, k] for k, h in MIC_SLOTS], axis=1).astype(np.float64) / 32768.0
    return mics, half[REF_SLOT[1]][:, REF_SLOT[0]].astype(np.float64) / 32768.0
HUB = profile.service("stt").rsplit("/", 1)[0] + "/hear"
UI_URL = "http://127.0.0.1:8080/api/event"    # the screen (and the LED ring): "I hear you"
VAD_MODEL = "/opt/eva/models/silero_vad.onnx"
GAIN = profile.tune("ear.ref_gain", 32.0)      # the reference tap (PGA 0.5 dB)
# the mics' PGA went 20 -> 36 dB (station-mics-init.sh, 2026-11-23): at 20 dB the
# ADC's own noise was 3.5x the microphones' and ate ~10 dB of SNR. 16 dB less here
# keeps the levels the VAD and the canceller were tuned on.
MIC_GAIN = profile.tune("ear.mic_gain", 32.0)   # with the mics' PGA at 20 dB (station-mics-init.sh)
START_P, END_P = profile.tune("ear.start_p", 0.5), profile.tune("ear.end_p", 0.35)        # speech probability thresholds
END_SILENCE = profile.tune("ear.end_silence", 0.8)                 # s of non-speech that ends a phrase (0.5, then 0.65 cut phrases at a breath)
TAIL_KEEP = profile.tune("ear.tail_keep", 0.3)                   # s of the trailing silence kept for STT
# ...unless only a word or two was said: "Ева, <pause> кто такая Ольга" was cut
# after "Ева" and the lone name came out as "ево" (test set 2026-11-19)
SHORT_HEAD, END_SILENCE_SHORT = profile.tune("ear.short_head", 0.9), profile.tune("ear.end_silence_short", 1.1)
MIN_SPEECH = profile.tune("ear.min_speech", 0.35)                 # s: less is a click - its audio is not sent
# length: a fragment past SOFT_MAX ends at the next short dip of the voice (a
# gap between words), past MAX_SPEECH anyway; either way it is "chained" and
# the hub glues it with its rest
SOFT_MAX, DIP = profile.tune("ear.soft_max", 12.0), profile.tune("ear.dip", 0.15)
MAX_SPEECH = profile.tune("ear.max_speech", 20.0)
PREROLL = profile.tune("ear.preroll_chunks", 20)                      # chunks kept before the start (640 ms): a quiet "Ева" at the start was cut
PLAYBACK_WINDOW = profile.tune("ear.playback_window", 1.5)             # s: while she talks, recognise in windows this long
OVERLAP = 19                      # chunks (~0.6 s) shared by consecutive windows: no word cut in half
MUSIC_FLAG = "/run/eva/music"          # app/music.py holds it while music plays
SPEAKING_FLAG = "/run/eva/speaking"   # the brain holds it while her voice plays
HEARING_FLAG = "/run/eva/hearing"     # held while a phrase is being heard: the brain waits for its rest


def hearing(on):
    try:
        if on:
            open(HEARING_FLAG, "w").close()
        elif os.path.exists(HEARING_FLAG):
            os.remove(HEARING_FLAG)
    except OSError:
        pass
REF_ACTIVE = profile.tune("ear.ref_active", 5e-4)                 # reference level (of full scale) = speakers playing
RECORD_FLAG = "/var/lib/eva/stt-record"   # exists: phrases are a test set (kept on the hub), Eva does not answer


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class EchoCanceller:
    """Partitioned-block FDAF NLMS, overlap-save. N-sample blocks, P partitions
    (filter length N*P). Adapts only on blocks where the reference really
    carries signal: between words it is near zero, and normalising by that
    blows the step up (the filter diverged live, 2026-11-18). A block whose
    output is louder than its input means divergence: the filter is reset."""

    def __init__(self, n=256, partitions=4, mu=0.5, adapt_floor=1e-6, reg=1e-3):
        # adapt_floor: mean square of the reference below which nothing is learnt;
        # reg: a small constant against division by a near-empty bin. Tied
        # together they over-regularised the filter: 22 dB of ERLE fell to 10.
        self.n, self.p, self.mu, self.floor, self.reg = n, partitions, mu, adapt_floor, reg
        self.W = np.zeros((partitions, n + 1), dtype=np.complex128)
        self.X = np.zeros((partitions, n + 1), dtype=np.complex128)
        self.xprev = np.zeros(n)
        self.pow = np.full(n + 1, 1.0)
        self.resets = 0

    def block(self, d, x):
        n = self.n
        Xb = np.fft.rfft(np.concatenate([self.xprev, x]))
        self.xprev = x
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = Xb
        y = np.fft.irfft((self.W * self.X).sum(axis=0))[n:]
        e = d - y
        ed, ee = float(np.mean(d * d)), float(np.mean(e * e))
        # truly diverged, or NaN (a comparison with NaN is never true: a NaN
        # filter went on for ever and poisoned the VAD - deaf for hours after
        # long loud music, 2026-11-20): start over
        if not np.isfinite(ee) or (ed > 0 and ee > 100 * ed):
            self.W[:] = 0
            self.X[:] = 0
            self.pow[:] = 1.0
            self.resets += 1
            return d
        if float(np.mean(x * x)) > self.floor:
            self.pow = 0.9 * self.pow + 0.1 * (np.abs(Xb) ** 2)
            E = np.fft.rfft(np.concatenate([np.zeros(n), e]))
            denom = self.pow * self.p + self.reg
            self.W += self.mu * np.conj(self.X) * E / denom
            # gradient constraint: keep the filter causal and N long per partition
            w = np.fft.irfft(self.W, axis=1)
            w[:, n:] = 0
            self.W = np.fft.rfft(w, axis=1)
        return e

    def process(self, d, x):
        out = np.empty_like(d)
        for i in range(0, len(d), self.n):
            out[i:i + self.n] = self.block(d[i:i + self.n], x[i:i + self.n])
        return out


class Vad:
    def __init__(self):
        o = ort.SessionOptions()
        o.intra_op_num_threads = 1
        o.inter_op_num_threads = 1
        self.s = ort.InferenceSession(VAD_MODEL, sess_options=o)
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.ctx = np.zeros((1, 64), dtype=np.float32)
        self.sr = np.array(SR, dtype=np.int64)
        self.resets = 0

    def __call__(self, chunk):
        x = np.concatenate([self.ctx, chunk[None, :].astype(np.float32)], axis=1)
        p, state = self.s.run(None, {"input": x, "state": self.state, "sr": self.sr})
        p = float(p[0][0])
        if not np.isfinite(p) or not np.isfinite(state).all():
            # a bad chunk must not stay in the recurrent state for ever
            self.state = np.zeros((2, 1, 128), dtype=np.float32)
            self.ctx = np.zeros((1, 64), dtype=np.float32)
            self.resets += 1
            return 0.0
        self.state = state
        self.ctx = x[:, -64:]
        return p


class HighPass:
    """Rumble filter: 4th-order Butterworth high-pass at 120 Hz (two biquads,
    continuous across chunks): -32 dB at 48 Hz, -0.7 dB at 150 Hz.
    The one-pole filter at ~60 Hz before it let a 46-49 Hz rumble (a machine
    in the room, 2026-11-23) through almost whole (-4 dB): it set the phrase
    normalisation and the VAD heard no speech - every phrase came out empty."""

    SOS = ((0.9402805365982765, -1.880561073196553, 0.9402805365982765, -1.9144610905921862, 0.9165887437442231),
           (1.0, -2.0, 1.0, -1.9624037752498193, 0.9645847099256387))   # b0 b1 b2 a1 a2, scipy butter(4, 120, fs=16000)

    def __init__(self):
        self.z = [[0.0, 0.0] for _ in self.SOS]

    def __call__(self, v):
        # plain floats and a list: indexing a numpy array per sample was 4x slower
        # on the Station's A53 (7.9 ms a chunk)
        out = np.asarray(v, dtype=np.float64).tolist()
        for (b0, b1, b2, a1, a2), z in zip(self.SOS, self.z):
            z1, z2 = z
            res = []
            append = res.append
            for x in out:                   # direct form II transposed
                y = b0 * x + z1
                z1 = b1 * x - a1 * y + z2
                z2 = b2 * x - a2 * y
                append(y)
            z[0], z[1] = z1, z2
            out = res
        return np.array(out)


def ui(state, text=None):
    """What hearing is doing, for the screen: on / stt / held / text / none.
    Fire and forget - the microphones must never wait for the screen."""
    ev = {"type": "hear", "state": state}
    if text:
        ev["text"] = text

    def post():
        try:
            urllib.request.urlopen(urllib.request.Request(
                UI_URL, data=json.dumps(ev, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"}), timeout=1).read()
        except OSError:
            pass
    threading.Thread(target=post, daemon=True).start()


# ---- to the hub ----
_out = queue.Queue()
SEQ = [0]


def send(seg, raw7, meta):
    """A fragment to the hub, in order (one sender). seg: float mono, raw7:
    samples x 7 float or None. A fragment is tried again while the hub comes
    back (~25 s: a deploy restarts it); a window over her voice is not."""
    SEQ[0] += 1
    meta = dict(meta, seq=SEQ[0], record=os.path.exists(RECORD_FLAG) or None)
    mono = np.clip(seg * 32767, -32767, 32767).astype("<i2").tobytes() if seg is not None else b""
    mics = np.clip(raw7 * 32768, -32768, 32767).astype("<i2").tobytes() if raw7 is not None else b""
    _out.put(("/fragment", mono + mics, {"X-Mono": str(len(mono) // 2),
                                         "X-Channels": str(raw7.shape[1]) if raw7 is not None else "0"}, meta))


def started(t):
    """The voice began (a held piece on the hub waits for its rest)."""
    _out.put(("/start", None, None, {"t": t}))


def sender():
    while True:
        path, body, hdr, meta = _out.get()
        meta["now"] = time.time()
        if body is None:
            data, h = json.dumps(meta).encode(), {"Content-Type": "application/json"}
        else:
            data, h = body, dict(hdr, **{"X-Meta": json.dumps(meta), "Content-Type": "application/octet-stream"})
        waits = [] if meta.get("window") or path == "/start" else [2, 4, 8, 10]
        while True:
            try:
                urllib.request.urlopen(urllib.request.Request(HUB + path, data=data, headers=h), timeout=10).read()
                break
            except OSError as e:
                if not waits:
                    log(f"hub {path} error:", e)
                    if path == "/fragment" and not meta.get("window"):
                        ui("none")
                    break
                w = waits.pop(0)
                log(f"hub {path} error: {e} - again in {w}s")
                time.sleep(w)


def main():
    vad, aec = Vad(), EchoCanceller(adapt_floor=1e-6)   # scaled signals: tested 21 dB steady
    threading.Thread(target=sender, daemon=True).start()

    proc = subprocess.Popen(["arecord", "-q", "-D", "hw:snddaudio0,0", "-c", "5", "-f", "S32_LE",
                             "-r", str(SR), "--period-size=256", "-t", "raw"],
                            stdout=subprocess.PIPE, bufsize=0)
    nbytes = CHUNK * 5 * 4
    hp_mic, hp_ref = HighPass(), HighPass()
    dump = open(os.environ["EVA_EAR_DUMP"], "wb") if os.environ.get("EVA_EAR_DUMP") else None
    pre = collections.deque(maxlen=PREROLL)
    seg, seg_ref, speaking, silence, playing_until = [], [], False, 0.0, 0.0
    seg_raw = []                          # the 7 raw mics of the fragment: its direction (on the hub)
    n_pre = 0
    stats_t, stats = time.time(), collections.Counter()
    log("listening")
    while True:
        raw = b""
        while len(raw) < nbytes:
            b = proc.stdout.read(nbytes - len(raw))
            if not b:
                sys.exit("capture ended")
            raw += b
        if dump:
            dump.write(raw)
        mics, ref = unpack(raw)
        # both signals through the same filters, without gaps: the canceller
        # needs the reference history continuous to model the echo path
        # what is heard: the mics in HEAR_MICS only. All 7 averaged made her deaf
        # (2026-11-23): some capsules have the opposite polarity (pair correlation
        # down to -0.38) and the sum cancelled the voice - VAD max 0.36 while
        # talking. All 7 still go to the hub for the direction.
        y = hp_mic(mics[:, HEAR_MICS].mean(axis=1) * MIC_GAIN)
        x = hp_ref(ref * GAIN)
        ref_on = float(np.sqrt(np.mean(x * x))) > REF_ACTIVE * GAIN
        now = time.time()
        if ref_on:
            playing_until = now + 0.3
        e = np.nan_to_num(aec.process(y, x), nan=0.0, posinf=0.0, neginf=0.0)
        # she is talking: the reference says so, or the brain does (the
        # reference channel has not always been where it should be)
        # music plays: the same short windows as over her voice, so «Ева…» and
        # «стоп» are caught over it (the reference does not always see the music)
        play_now = ref_on or now < playing_until or os.path.exists(SPEAKING_FLAG) or os.path.exists(MUSIC_FLAG)
        # HER voice (not music): what counts as her echo - the share of the phrase
        # under it ("playback") drops it as an echo here and lets only a stop or her
        # name through in the brain. Music counted too and every command said over
        # it was thrown away as her echo (2026-11-23)
        her_now = os.path.exists(SPEAKING_FLAG) or (now < playing_until and not os.path.exists(MUSIC_FLAG))
        # energy before / after the canceller: what it removed was Eva's own echo
        en_in, en_out = float(np.mean(y * y)), float(np.mean(e * e))
        # raw level of the microphones (before any processing): tells a deaf
        # board from a deaf algorithm
        stats["mic_peak"] = max(stats["mic_peak"], float(np.abs(mics).max()))
        stats["vad_max"] = max(stats["vad_max"], 0)
        e = np.clip(e, -1.0, 1.0).astype(np.float32)
        p = vad(e)
        stats["vad_max"] = max(stats["vad_max"], p)
        stats["chunks"] += 1
        stats["speech"] += p > START_P
        stats["play"] += now < playing_until
        if not speaking:
            pre.append((e, her_now, en_in, en_out, mics.astype(np.float32)))
            if p > START_P:
                speaking, silence = True, 0.0
                hearing(True)
                seg = [c for c, *_ in pre]
                seg_ref = [(r, i, o) for _, r, i, o, _ in pre]
                seg_raw = [m for *_, m in pre]
                n_pre = len(seg)
                pre.clear()
                t_start = now - len(seg) * CHUNK / SR
                t_speech = now - CHUNK / SR          # when the voice itself began (no pre-roll)
                started(now)
                if not play_now:
                    ui("on")
        else:
            seg.append(e)
            seg_ref.append((her_now, en_in, en_out))
            seg_raw.append(mics.astype(np.float32))
            silence = silence + CHUNK / SR if p < END_P else 0.0
            dur = len(seg) * CHUNK / SR
            # windows only while HER voice plays (its echo keeps the VAD on). Over
            # music they cut every phrase into 1.5 s pieces recognised apart -
            # «педикюра на зрении ева ева что нави установи» (2026-11-23); the
            # VAD hardly reacts to music, so phrases end by silence there too.
            if her_now and dur >= PLAYBACK_WINDOW and silence < END_SILENCE:
                # she is talking: her echo keeps the VAD on, so a "стоп" would be
                # glued into one long phrase of her words. Short overlapping
                # windows go to recognition instead, each on its own.
                meta = {"dur": round(dur, 2), "start": t_start, "speech_start": t_speech, "end": now,
                        "playback": 1.0, "residual": None, "window": True}
                send(np.concatenate(seg), None, meta)
                seg, seg_ref, seg_raw = seg[-OVERLAP:], seg_ref[-OVERLAP:], seg_raw[-OVERLAP:]
                n_pre = 0
                t_start = t_speech = now - len(seg) * CHUNK / SR
                continue
            voiced = (len(seg) - n_pre) * CHUNK / SR - silence      # without the pre-roll
            end_silence = END_SILENCE_SHORT if voiced < SHORT_HEAD and not play_now else END_SILENCE
            # a pause ends the fragment; a long one also ends at a short dip of the
            # voice (between words) or at MAX_SPEECH - then it is "chained"
            paused = silence >= end_silence
            if paused or dur >= MAX_SPEECH or (dur >= SOFT_MAX and silence >= DIP):
                speaking = False
                hearing(False)
                speech = dur - silence
                played = [(i, o) for r, i, o in seg_ref if r]
                playback = len(played) / len(seg_ref)
                # left after the canceller / heard: near 0 = it was all her echo
                residual = (sum(o for _, o in played) / (sum(i for i, _ in played) + 1e-12)) if played else 1.0
                meta = {"dur": round(speech, 2), "start": t_start, "speech_start": t_speech, "end": now,
                        "playback": round(playback, 3), "residual": round(residual, 3), "chained": not paused}
                if speech < MIN_SPEECH:
                    send(None, None, meta)          # a click: no sound, the hub only learns it ended
                    ui("none")
                else:
                    # keep 0.3 s after the VAD dropped: soft endings ("-ть", "-с") fade
                    # out after it, cutting 60 % of the silence cut them off (2026-11-23)
                    keep_n = int(len(seg) * CHUNK - max(0.0, silence - TAIL_KEEP) * SR)
                    send(np.concatenate(seg)[:keep_n], np.concatenate(seg_raw)[:keep_n], meta)
                    if playback < 0.5:
                        ui("stt")
        if now - stats_t > 60:
            log(f"level: speech {stats['speech']}/{stats['chunks']} chunks, playback {stats['play']}, "
                f"resets aec {aec.resets} vad {vad.resets}, mic peak {20 * np.log10(stats['mic_peak'] + 1e-12):.0f} dBFS, "
                f"vad max {stats['vad_max']:.2f}")
            stats_t, stats = now, collections.Counter()


if __name__ == "__main__":
    main()
