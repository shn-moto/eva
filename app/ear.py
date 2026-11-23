#!/usr/bin/env python3
"""Eva's hearing: microphones -> echo cancelling -> phrases -> text -> brain.

Capture: 5 channels at 16 kHz from the mic board (snddaudio0):
    ch1..ch3  microphones (averaged)
    ch4       the amplifier tap: exactly what the speakers play, sampled by the
              same ADC clock as the mics - the echo canceller's reference
Echo cancelling: partitioned-block frequency-domain NLMS (overlap-save),
1024 taps, removes Eva's own voice (and music) from the mic signal, so she
can be interrupted while she talks.
Phrases: Silero VAD on the echo-cancelled signal, 0.5 s of silence ends one.
Text: GigaAM on shn-linux. Every phrase goes to the brain (127.0.0.1:8081)
with how much of it overlapped Eva's own playback.
"""
import collections
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import wave

import numpy as np
import onnxruntime as ort

import names
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
STT_URL = profile.service("stt")
BRAIN_URL = os.environ.get("EVA_BRAIN_URL", "http://127.0.0.1:8081/utterance")
UI_URL = "http://127.0.0.1:8080/api/event"    # the screen (and the LED ring): "I hear you"
VAD_MODEL = "/opt/eva/models/silero_vad.onnx"
GAIN = profile.tune("ear.ref_gain", 32.0)      # the reference tap (PGA 0.5 dB)
# the mics' PGA went 20 -> 36 dB (station-mics-init.sh, 2026-11-23): at 20 dB the
# ADC's own noise was 3.5x the microphones' and ate ~10 dB of SNR. 16 dB less here
# keeps the levels the VAD and the canceller were tuned on.
MIC_GAIN = profile.tune("ear.mic_gain", 5.07)
START_P, END_P = profile.tune("ear.start_p", 0.5), profile.tune("ear.end_p", 0.35)        # speech probability thresholds
END_SILENCE = profile.tune("ear.end_silence", 0.65)                # s of non-speech that ends a phrase (0.5 cut phrases at a breath)
TAIL_KEEP = profile.tune("ear.tail_keep", 0.3)                   # s of the trailing silence kept for STT
# ...unless only a word or two was said: "Ева, <pause> кто такая Ольга" was cut
# after "Ева" and the lone name came out as "ево" (test set 2026-11-19)
SHORT_HEAD, END_SILENCE_SHORT = profile.tune("ear.short_head", 0.9), profile.tune("ear.end_silence_short", 1.1)
MIN_SPEECH, MAX_SPEECH = profile.tune("ear.min_speech", 0.35), profile.tune("ear.max_speech", 15.0)
PREROLL = profile.tune("ear.preroll_chunks", 20)                      # chunks kept before the start (640 ms): a quiet "Ева" at the start was cut
PLAYBACK_WINDOW = profile.tune("ear.playback_window", 1.5)             # s: while she talks, recognise in windows this long
OVERLAP = 19                      # chunks (~0.6 s) shared by consecutive windows: no word cut in half
MUSIC_FLAG = "/run/eva/music"          # app/music.py holds it while music plays
SPEAKING_FLAG = "/run/eva/speaking"   # the brain holds it while her voice plays
ECHO_RESIDUAL = profile.tune("ear.echo_residual", 0.1)               # canceller left <10% of the energy: her own echo
REF_ACTIVE = profile.tune("ear.ref_active", 5e-4)                 # reference level (of full scale) = speakers playing
RECORD_FLAG = "/var/lib/eva/stt-record"   # exists: save phrases as a test set, do not answer
RECORD_DIR = "/var/lib/eva/stt-samples"


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
    """One-pole DC / rumble filter (~60 Hz at 16 kHz), continuous across chunks."""

    def __init__(self):
        self.px = self.py = 0.0

    def __call__(self, v):
        out = np.empty_like(v)
        px, py = self.px, self.py
        for i, s in enumerate(v):
            py = 0.977 * (py + s - px)
            px = s
            out[i] = py
        self.px, self.py = px, py
        return out


DOA_URL = STT_URL.rsplit("/", 1)[0] + "/doa"


def direction(raw7):
    """Direction fingerprint of a phrase from its 7 raw mics (shn-linux):
    {"lags": 21 pair delays, "peak", "blocks": per 0.5 s}, or None."""
    try:
        body = np.clip(raw7 * 32768, -32768, 32767).astype(np.int16).tobytes()
        req = urllib.request.Request(DOA_URL, data=body, headers={"Content-Type": "application/octet-stream",
                                                                    "X-Channels": str(raw7.shape[1])})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read()) or None
    except Exception as e:
        log("doa error:", e)
        return None


def stt(pcm16, split=False):
    """-> {"text", "split"?: {"t", "win", "whole", "embs", "texts"}} (stt_server.py)"""
    req = urllib.request.Request(STT_URL + ("?split=1" if split else ""), data=pcm16.tobytes(),
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.loads(r.read())
    res["text"] = (res.get("text") or "").strip()
    return res


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


def send_brain(item):
    req = urllib.request.Request(BRAIN_URL, data=json.dumps(item, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except OSError as e:
        log("brain not reachable:", e)


_SPK = None
_SPK_LOCK = threading.Lock()
SPK_MODEL = "/opt/eva/models/campplus.onnx"
SPK_SECONDS = profile.tune("ear.speaker_seconds", 3.0)                 # the first seconds are enough to know a voice


def voice_embedding(audio):
    """CAM++ embedding (192 floats) of who is speaking, or None."""
    global _SPK
    try:
        with _SPK_LOCK:
            if _SPK is None:
                import sherpa_onnx
                _SPK = sherpa_onnx.SpeakerEmbeddingExtractor(
                    sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=SPK_MODEL, num_threads=2))
            st = _SPK.create_stream()
            st.accept_waveform(SR, audio[: int(SR * SPK_SECONDS)].astype(np.float32))
            st.input_finished()
            e = np.array(_SPK.compute(st), dtype=np.float32)
        return (e / (np.linalg.norm(e) + 1e-9)).round(5).tolist()
    except Exception as e:  # model missing or broken: no voice id, the rest goes on
        log("voice id error:", e)
        return None


SELF_PROFILE = "/var/lib/eva/voices/eva-self.json"   # her own voice (xAI "eve"), from her TTS cache
SPEAKERS = "/var/lib/eva/voices/speakers.json"      # the family's voices (written by the brain)
TTS_CACHE = "/var/cache/eva/tts"
_voices = {"self": [], "family": [], "named": {}, "loaded": 0.0}


def _load_wav16(path):
    with wave.open(path) as w:
        sr, a = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    a = a.astype(np.float32) / 32768.0
    if sr != SR:
        a = np.interp(np.arange(0, len(a), sr / SR), np.arange(len(a)), a).astype(np.float32)
    return a


def self_profile():
    """Embeddings of her own voice: her longest cached sentences, made once."""
    try:
        return json.load(open(SELF_PROFILE))
    except (OSError, ValueError):
        pass
    wavs = sorted((os.path.join(TTS_CACHE, n) for n in os.listdir(TTS_CACHE) if n.endswith(".wav")),
                  key=os.path.getsize, reverse=True)[:20]
    embs = [e for e in (voice_embedding(_load_wav16(w)) for w in wavs) if e]
    os.makedirs(os.path.dirname(SELF_PROFILE), exist_ok=True)
    with open(SELF_PROFILE, "w") as f:
        json.dump(embs, f)
    log(f"own voice profile: {len(embs)} samples")
    return embs


def voice_sims(emb):
    """-> (similarity to her own voice, best similarity to a family voice)"""
    if time.time() - _voices["loaded"] > 300:
        try:
            fam = json.load(open(SPEAKERS))
            _voices["named"] = {k: [np.asarray(e, dtype=np.float32) / (np.linalg.norm(e) + 1e-9) for e in v]
                                for k, v in fam.items()}
            _voices["family"] = [e for v in _voices["named"].values() for e in v]
        except (OSError, ValueError):
            pass
        _voices["loaded"] = time.time()
    e = np.asarray(emb, dtype=np.float32)
    best = lambda embs: max((float(e @ (x / (np.linalg.norm(x) + 1e-9))) for x in embs), default=0.0)
    return round(best(_voices["self"]), 3), round(best(_voices["family"]), 3)


def save_sample(pcm, seg, peak, meta):
    """Test-set recording (docs/BACKLOG.md): the exact PCM the STT got, plus the
    level it had before normalising - quiet speech is part of what is measured."""
    os.makedirs(RECORD_DIR, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta["speech_start"])) + f"-{int(meta['speech_start'] * 1000) % 1000:03d}"
    with wave.open(os.path.join(RECORD_DIR, name + ".wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) + 1e-12
    info = {k: meta[k] for k in ("dur", "text", "stt_ms", "playback", "residual")}
    info.update(peak_db=round(20 * np.log10(peak + 1e-12), 1), rms_db=round(20 * np.log10(rms), 1))
    with open(os.path.join(RECORD_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)


def recognise(seg, meta):
    """In a thread: STT (and, for a phrase, who said it) never hold up listening."""
    t = time.time()
    if "held" in meta:                    # the rest of a held piece: glue only one voice
        p0 = float(np.abs(seg).max()) or 1.0
        seg, meta, _ = JOIN.glue(seg, meta, voice_embedding(seg / p0 * 0.9))
    peak = float(np.abs(seg).max()) or 1.0
    norm = seg / peak * 0.9
    pcm = np.clip(norm * 32767, -32767, 32767).astype(np.int16)
    emb = {}
    raw7 = meta.pop("mics", None)
    th_doa = None
    if raw7 is not None and not meta.get("window"):
        th_doa = threading.Thread(target=lambda: emb.update(doa=direction(raw7)), daemon=True)
        th_doa.start()
    # windows too: is it her own voice (echo) or somebody talking over her?
    th = threading.Thread(target=lambda: emb.update(v=voice_embedding(norm)), daemon=True)
    th.start()
    split = not meta.get("window") and meta["dur"] >= SPLIT_MIN and not os.path.exists(RECORD_FLAG)
    try:
        res = stt(pcm, split=split)
    except OSError as e:
        log("stt error:", e)
        if not meta.get("window"):
            ui("none")
        return
    th.join(timeout=3)
    if th_doa:
        th_doa.join(timeout=2)
        if emb.get("doa"):
            meta["doa"] = emb["doa"]
    meta["stt_ms"] = int(1000 * (time.time() - t))
    ch = res.get("split")
    if ch and split_ok(ch):
        # two people in one phrase: each part is a phrase of its own, with its own voice
        cut = int(ch["t"] * SR)
        log(f"split at {ch['t']}s: another voice (windows {ch['win']}, parts {ch['whole']}): "
            f"{ch['texts'][0]!r} | {ch['texts'][1]!r}")
        t_cut = meta["start"] + ch["t"]
        parts = [(seg[:cut], dict(meta, end=t_cut, dur=round(ch["t"], 2), split=1)),
                 (seg[cut:], dict(meta, start=t_cut, speech_start=t_cut, dur=round(meta["dur"] - ch["t"], 2), split=2))]
        for (s_, m_), text, e in zip(parts, ch["texts"], ch["embs"]):
            deliver(s_, m_, text, e, None, peak, hold=False)
        return
    deliver(seg, meta, res.get("text") or "", emb.get("v"), pcm, peak)


def deliver(seg, meta, text, v, pcm, peak, hold=True):
    """A recognised phrase (or a part of one) -> the brain."""
    raw, text = text, names.fix(text) if text else text
    if v:
        meta["self_sim"], meta["fam_sim"] = voice_sims(v)
    meta.update(text=text, raw=raw, voice=None if meta.get("window") else v)
    log(f"{'window' if meta.get('window') else 'phrase'} {meta['dur']:.1f}s playback {meta['playback']:.0%} residual {meta['residual']} "
        f"stt {meta['stt_ms']}ms self {meta.get('self_sim')} fam {meta.get('fam_sim')}: {text!r}"
        + (f" doa {meta['doa']['lags'][:6]} peak {meta['doa']['peak']}" if meta.get("doa") else ""))
    if os.path.exists(RECORD_FLAG):
        if not meta.get("window") and pcm is not None:
            save_sample(pcm, seg, peak, meta)
        return                            # recording a test set: Eva stays out of it
    # her own echo is never held: joined with his next phrase it swallowed his
    # "хорошо" (echo "снова я" held, the merge came out "снова я" again, 2026-11-19)
    echo = meta.get("playback", 0) > 0.3 or (meta.get("self_sim") or 0) >= 0.45
    if hold and text and not meta.get("window") and not echo and incomplete(text):
        JOIN.hold(seg, meta)              # a sentence cut in a pause: wait for its rest
        ui("held", text)
    elif text:
        # what was heard over the music or her voice is mostly the song / her
        # echo: not shown under the face (it showed gibberish of the lyrics)
        if not meta.get("window") and meta.get("playback", 0) < 0.3:
            ui("text", text)
        send_brain(meta)
    elif not meta.get("window"):
        ui("none")


# Two people in one phrase (no pause between them): shn-linux finds the
# likeliest change of voice (stt_server.speaker_change); taken when the two
# parts are clearly different voices, or two different family voices.
# Calibrated 2026-11-19: ~half of such phrases caught, ~2% false splits.
SPLIT_MIN = profile.tune("ear.split_min", 3.0)
SPLIT_SURE = profile.tune("ear.split_sure", 0.3)                  # parts this unlike: two people (0 false splits of 25 single phrases)


def split_ok(ch):
    if ch["whole"] < SPLIT_SURE:
        return True
    a, b = (who(e) for e in ch["embs"])
    return a is not None and b is not None and a != b


def who(emb):
    """The family member this voice is (best match >= 0.5, 0.1 ahead of the next), or None."""
    voice_sims(emb)                       # keeps the profiles fresh
    e = np.asarray(emb, dtype=np.float32)
    best = sorted(((max(float(e @ x) for x in v), k) for k, v in _voices["named"].items() if v), reverse=True)
    if not best or best[0][0] < 0.5 or (len(best) > 1 and best[0][0] - best[1][0] < 0.1):
        return None
    return best[0][1]


# words a finished sentence does not end with: "Ева," / "погода в" / "скажи что"
OPEN_END = set("""ева эва и а но или в во на с со к ко у о об по за из от до для при про без над под
что чтобы как когда если потому поэтому который которая которые это этот эта мне меня тебе тебя
мой моя наш наша ты я он она мы вы они не ну вот слушай скажи покажи найди""".split())


def incomplete(text):
    words = text.lower().split()
    return bool(words) and words[-1] in OPEN_END


class Joiner:
    """Finds sentences, not pieces: a phrase whose text ends open ("Ева",
    "какая погода в") is held back for JOIN_GAP; if the voice goes on, both
    pieces are recognised again as one audio, otherwise the piece goes as is."""

    JOIN_GAP = 1.0                    # s after the piece ended (on top of END_SILENCE)
    REST_WAIT = 12.0                  # s more for a rest that began: then the piece goes alone
    SAME_VOICE = 0.5                  # CAM++ similarity of the two pieces to glue them

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = None           # (audio, meta, deadline, joining)
        self.last_end = 0.0           # when the last phrase ended (before its STT is back)
        self.cont = 0.0               # when a phrase started soon after it

    def ended(self, now):
        with self.lock:
            self.last_end = now

    def hold(self, seg, meta):
        """Called when STT is back, which may be after the rest of the sentence
        already began (or even ended) - the timeline decides, not the order."""
        with self.lock:
            if self.last_end > meta["end"]:       # the rest is already over: too late to glue
                p = None
            else:
                joining = meta["end"] < self.cont <= meta["end"] + self.JOIN_GAP
                p = self.pending = [seg, meta, meta["end"] + self.JOIN_GAP, joining]
        if p is None:
            send_brain(meta)
            return
        log(f"held (open end): {meta['text']!r}{' - its rest is coming' if p[3] else ''}")
        threading.Thread(target=self._expire, args=(self.pending,), daemon=True).start()

    def _expire(self, p):
        time.sleep(max(0.0, p[2] - time.time()))
        with self.lock:
            if self.pending is not p:
                return
            if not p[3]:
                self.pending = None
        if p[3]:
            # its "rest" began but may never come as a phrase (a window over her
            # voice, dropped as echo): the piece is never lost, it goes on its own
            time.sleep(self.REST_WAIT)
            with self.lock:
                if self.pending is not p:
                    return
                self.pending = None
            log(f"held piece sent alone (its rest never came): {p[1]['text']!r}")
        send_brain(p[1])

    def started(self, now):
        """A new phrase began: it continues the held piece if it came in time."""
        with self.lock:
            if now - self.last_end <= self.JOIN_GAP:
                self.cont = now
            if self.pending and now <= self.pending[2]:
                self.pending[3] = True

    def merge(self, seg, meta):
        """The new phrase ended: if it continues the held piece, the piece rides
        along with it to recognise() - glued there only if it is the same voice
        (pieces of two people were glued into one phrase, 2026-11-19)."""
        with self.lock:
            p, self.pending = self.pending, None
        if not p or not p[3]:
            if p:
                with self.lock:       # not a continuation: put it back for _expire
                    self.pending = self.pending or p
            return seg, meta
        meta["held"] = p
        return seg, meta

    def glue(self, seg, meta, v):
        """In recognise(), the voice of the rest known: one sentence of one voice
        -> (seg, meta) glued; another voice -> the piece goes alone first."""
        p = meta.pop("held")
        hv = p[1].get("voice")
        unit = lambda x: np.asarray(x, dtype=np.float32) / (np.linalg.norm(x) + 1e-9)
        sim = float(unit(hv) @ unit(v)) if hv is not None and v is not None else 0.0
        if sim < self.SAME_VOICE:
            log(f"not joined: another voice ({sim:.2f}) after {p[1]['text']!r}")
            send_brain(p[1])
            return seg, meta, False
        gap = np.zeros(int(0.15 * SR), dtype=np.float32)
        m = dict(meta, start=p[1]["start"], speech_start=p[1]["speech_start"],
                 dur=round(p[1]["dur"] + meta["dur"], 2), joined=True)
        log(f"joined with {p[1]['text']!r} (same voice {sim:.2f})")
        return np.concatenate([p[0], gap, seg]), m, True

    def release(self):
        """The continuation turned out to be nothing (echo, a cough): send the piece."""
        with self.lock:
            p, self.pending = self.pending, None
        if p and p[3]:
            send_brain(p[1])
        elif p:
            with self.lock:
                self.pending = self.pending or p


JOIN = Joiner()


def main():
    vad, aec = Vad(), EchoCanceller(adapt_floor=1e-6)   # scaled signals: tested 21 dB steady
    _voices["self"] = [np.asarray(e, dtype=np.float32) for e in self_profile()]
    voice_embedding(np.zeros(SR, dtype=np.float32))   # load CAM++ now: lazily it cost the first phrase 3 s and its voice
    proc = subprocess.Popen(["arecord", "-q", "-D", "hw:snddaudio0,0", "-c", "5", "-f", "S32_LE",
                             "-r", str(SR), "--period-size=256", "-t", "raw"],
                            stdout=subprocess.PIPE, bufsize=0)
    nbytes = CHUNK * 5 * 4
    hp_mic, hp_ref = HighPass(), HighPass()
    dump = open(os.environ["EVA_EAR_DUMP"], "wb") if os.environ.get("EVA_EAR_DUMP") else None
    pre = collections.deque(maxlen=PREROLL)
    seg, seg_ref, speaking, silence, playing_until = [], [], False, 0.0, 0.0
    seg_raw = []                          # the 7 raw mics of the phrase: its direction (stt_server /doa)
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
        # talking. All 7 still go to the direction (raw7 -> /doa).
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
            pre.append((e, play_now, en_in, en_out, mics.astype(np.float32)))
            if p > START_P:
                speaking, silence = True, 0.0
                seg = [c for c, *_ in pre]
                seg_ref = [(r, i, o) for _, r, i, o, _ in pre]
                seg_raw = [m for *_, m in pre]
                n_pre = len(seg)
                pre.clear()
                t_start = now - len(seg) * CHUNK / SR
                t_speech = now - CHUNK / SR          # when the voice itself began (no pre-roll)
                JOIN.started(now)
                if not play_now:
                    ui("on")
        else:
            seg.append(e)
            seg_ref.append((play_now, en_in, en_out))
            seg_raw.append(mics.astype(np.float32))
            silence = silence + CHUNK / SR if p < END_P else 0.0
            dur = len(seg) * CHUNK / SR
            # windows only while HER voice plays (its echo keeps the VAD on). Over
            # music they cut every phrase into 1.5 s pieces recognised apart -
            # «педикюра на зрении ева ева что нави установи» (2026-11-23); the
            # VAD hardly reacts to music, so phrases end by silence there too.
            her_voice = os.path.exists(SPEAKING_FLAG) or (now < playing_until and not os.path.exists(MUSIC_FLAG))
            if her_voice and dur >= PLAYBACK_WINDOW and silence < END_SILENCE:
                # she is talking: her echo keeps the VAD on, so a "стоп" would be
                # glued into one long phrase of her words. Short overlapping
                # windows go to recognition instead, each on its own.
                meta = {"dur": round(dur, 2), "start": t_start, "speech_start": t_speech, "end": now,
                        "playback": 1.0, "residual": None, "window": True}
                threading.Thread(target=recognise, args=(np.concatenate(seg), meta), daemon=True).start()
                seg, seg_ref, seg_raw = seg[-OVERLAP:], seg_ref[-OVERLAP:], seg_raw[-OVERLAP:]
                n_pre = 0
                t_start = t_speech = now - len(seg) * CHUNK / SR
                continue
            voiced = (len(seg) - n_pre) * CHUNK / SR - silence      # without the pre-roll
            end_silence = END_SILENCE_SHORT if voiced < SHORT_HEAD and not play_now else END_SILENCE
            if silence >= end_silence or dur >= MAX_SPEECH:
                speaking = False
                speech = dur - silence
                played = [(i, o) for r, i, o in seg_ref if r]
                playback = len(played) / len(seg_ref)
                # left after the canceller / heard: near 0 = it was all her echo
                residual = (sum(o for _, o in played) / (sum(i for i, _ in played) + 1e-12)) if played else 1.0
                if speech >= MIN_SPEECH and playback > 0.5 and residual < ECHO_RESIDUAL:
                    log(f"echo dropped: {speech:.1f}s, residual {residual:.3f}")
                    JOIN.release()
                    ui("none")
                elif speech < MIN_SPEECH:
                    JOIN.release()
                    ui("none")
                else:
                    meta = {"dur": round(speech, 2), "start": t_start, "speech_start": t_speech, "end": now,
                            "playback": playback, "residual": round(residual, 3)}
                    # keep 0.3 s after the VAD dropped: soft endings ("-ть", "-с") fade
                    # out after it, cutting 60 % of the silence cut them off (2026-11-23)
                    keep_n = int(len(seg) * CHUNK - max(0.0, silence - TAIL_KEEP) * SR)
                    s = np.concatenate(seg)[:keep_n]
                    raw7 = np.concatenate(seg_raw)[:keep_n]
                    s, meta = JOIN.merge(s, meta)
                    meta["mics"] = raw7
                    JOIN.ended(now)
                    ui("stt")
                    threading.Thread(target=recognise, args=(s, meta), daemon=True).start()
        if now - stats_t > 60:
            log(f"level: speech {stats['speech']}/{stats['chunks']} chunks, playback {stats['play']}, "
                f"resets aec {aec.resets} vad {vad.resets}, mic peak {20 * np.log10(stats['mic_peak'] + 1e-12):.0f} dBFS, "
                f"vad max {stats['vad_max']:.2f}")
            stats_t, stats = now, collections.Counter()


if __name__ == "__main__":
    main()
