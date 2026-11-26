"""The ear's signal processing, on the hub (docs/EAR-AUDIT-2026-11-26.md).

The Station is hardware only: it streams its raw capture here, untouched, over one TCP
connection (app/ear.py). Everything computed from the samples happens here:

    raw 5 x S32 words (7 mics + the amplifier taps, one ADC clock)
      -> the mics mixed (HEAR_MICS), high-pass
      -> WebRTC audio processing (AEC3 with the amplifier tap as the reference,
         noise suppression) - the canceller of Chrome and PipeWire, not our own
      -> Silero VAD -> fragments cut by pauses and length (the rules of the old ear.py)
      -> hear.STATION (in process): text, direction, who, echo, sentences.

Stream: the Station sends one JSON line {"v": 1, "sr", "words", "block"} and then blocks of
1 flag byte + `block` frames of `words` little-endian int32. Flags: 1 music plays, 2 her
voice plays, 4 the test-set recording is on - read on the Station per block, so they are
in step with the sound.

The black box: the last BOX_S seconds as processed (mic, reference, output, VAD) are kept;
POST /hear/box writes them to /log/ear-box/, and the ear writes them itself when its VAD
looks deaf over music.
"""
import collections
import json
import os
import socket
import threading
import time

import numpy as np
import onnxruntime as ort
from livekit import rtc

import ear_null
import hear

PORT = int(os.environ.get("EAR_PORT", "8912"))
SR = 16000
CHUNK = 512                      # 32 ms, Silero's frame
APM_N = 160                      # 10 ms, WebRTC's frame
VAD_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silero_vad.onnx")
BOX_DIR = os.path.join(os.environ.get("STT_LOG_DIR", "/log"), "ear-box")


def _f(name, default):
    return float(os.environ.get("EAR_" + name.upper(), default))


# Capture: 5 words of 32 bits, each = TWO 16-bit TDM slots (mapped 2026-11-23 by muting each
# ADC channel in turn): word k holds ADC(k-1 mod 5), high half = its right channel, low = left.
#   word 0: hi ADC4 R tap   lo ADC4 L tap
#   word 1-3: ADC0-2, both halves mics
#   word 4: hi ADC3 R tap (the speaker amplifier = the canceller's reference)   lo ADC3 L mic
MIC_SLOTS = ((1, "h"), (1, "l"), (2, "h"), (2, "l"), (3, "h"), (3, "l"), (4, "l"))
REF_SLOT = (4, "h")
HEAR_MICS = [0, 2, 4]            # mixed for listening: some capsules have the opposite polarity

# after the canceller: the levels the VAD and STT were tuned on. 32 with the mics' PGA at 20 dB;
# 10 with it at 30 dB (station-mics-init.sh, 2026-11-26: less ADC hiss under quiet speech)
MIC_GAIN = _f("mic_gain", 10.0)
LOUD = _f("loud", 0.3)           # raw peak (full scale 1) counted as "loud" by the stuck-VAD guard
REF_GAIN = _f("ref_gain", 32.0)  # the tap (PGA 0.5 dB) is ~-60 dBFS: up to where AEC3 sees it
REF_ACTIVE = _f("ref_active", 5e-4) * REF_GAIN
# The tap always carries the ADC's own noise (~-61 dBFS after REF_GAIN). Given to AEC3 as it is,
# that noise looked like "the speaker plays" and AEC3 suppressed everything said in the room as
# echo: on 2026-11-26 21:12 the VAD found 3 speech chunks in a minute where its input had 106.
# The reference is zero while the speaker is silent (a 10 ms frame below REF_GATE), kept open
# REF_HOLD frames after the last loud one so her voice's quiet tails still count.
REF_GATE = _f("ref_gate", 0.003)
REF_HOLD = int(_f("ref_hold", 20))
START_P, END_P = _f("start_p", 0.5), _f("end_p", 0.35)
END_SILENCE = _f("end_silence", 0.8)
TAIL_KEEP = _f("tail_keep", 0.3)
SHORT_HEAD, END_SILENCE_SHORT = _f("short_head", 0.9), _f("end_silence_short", 1.1)
MIN_SPEECH = _f("min_speech", 0.35)
SOFT_MAX, DIP = _f("soft_max", 12.0), _f("dip", 0.15)
MAX_SPEECH = _f("max_speech", 20.0)
PREROLL = int(_f("preroll_chunks", 20))
PLAYBACK_WINDOW = _f("playback_window", 1.5)
OVERLAP = 19                     # chunks (~0.6 s) shared by consecutive windows over her voice
VAD_STUCK = 3.0                  # s loud with no voice at all: the VAD's state starts over
BOX_S = 60
BOX_AUTO_EVERY = 600.0

STATE = {"connected": False, "peer": None, "since": None, "minute": None}


def log(*a):
    print(time.strftime("%H:%M:%S"), "ear:", *a, flush=True)


def unpack(raw):
    """-> (mics: n x 7 int16, ref: n int16)."""
    w = np.frombuffer(raw, dtype="<i4").reshape(-1, 5)
    hi = (w >> 16).astype(np.int16)
    lo = (w & 0xFFFF).astype(np.uint16).view(np.int16)
    half = {"h": hi, "l": lo}
    mics = np.stack([half[h][:, k] for k, h in MIC_SLOTS], axis=1)
    return mics, half[REF_SLOT[1]][:, REF_SLOT[0]]


class HighPass:
    """4th-order Butterworth high-pass at 120 Hz (two biquads, continuous across chunks)
    against a 46-49 Hz rumble of a machine in the room (2026-11-23)."""
    SOS = ((0.9402805365982765, -1.880561073196553, 0.9402805365982765, -1.9144610905921862, 0.9165887437442231),
           (1.0, -2.0, 1.0, -1.9624037752498193, 0.9645847099256387))

    def __init__(self):
        self.z = [[0.0, 0.0] for _ in self.SOS]

    def __call__(self, v):
        out = np.asarray(v, dtype=np.float64).tolist()
        for (b0, b1, b2, a1, a2), z in zip(self.SOS, self.z):
            z1, z2 = z
            res = []
            append = res.append
            for x in out:
                y = b0 * x + z1
                z1 = b1 * x - a1 * y + z2
                z2 = b2 * x - a2 * y
                append(y)
            z[0], z[1] = z1, z2
            out = res
        return np.array(out)


class Vad:
    def __init__(self):
        o = ort.SessionOptions()
        o.intra_op_num_threads = 1
        o.inter_op_num_threads = 1
        self.s = ort.InferenceSession(VAD_MODEL, sess_options=o)
        self.reset()
        self.sr = np.array(SR, dtype=np.int64)
        self.resets = 0

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.ctx = np.zeros((1, 64), dtype=np.float32)

    def __call__(self, chunk):
        x = np.concatenate([self.ctx, chunk[None, :].astype(np.float32)], axis=1)
        p, state = self.s.run(None, {"input": x, "state": self.state, "sr": self.sr})
        p = float(p[0][0])
        if not np.isfinite(p) or not np.isfinite(state).all():
            self.reset()
            self.resets += 1
            return 0.0
        self.state = state
        self.ctx = x[:, -64:]
        return p


class Front:
    """Mic + reference in, the echo-cancelled, denoised mic out (WebRTC APM, 10 ms frames)."""

    def __init__(self):
        self.apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True,
                                             high_pass_filter=True, auto_gain_control=False)
        self.apm.set_stream_delay_ms(0)        # the taps are on the mics' own TDM bus
        self.hp = HighPass()
        self.mic = np.zeros(0, dtype=np.int16)
        self.ref = np.zeros(0, dtype=np.int16)
        self.hold = 0
        self.silent = np.zeros(APM_N, dtype=np.int16)

    def process(self, mic, ref):
        """mic, ref: int16 at the ADC's level. -> the processed mic (int16), any length."""
        self.mic = np.concatenate([self.mic, np.clip(self.hp(mic), -32768, 32767).astype(np.int16)])
        self.ref = np.concatenate([self.ref, np.clip(ref.astype(np.float64) * REF_GAIN, -32768, 32767).astype(np.int16)])
        out = []
        while len(self.mic) >= APM_N:
            rf = self.ref[:APM_N]
            if float(np.sqrt(np.mean((rf.astype(np.float64) / 32768) ** 2))) > REF_GATE:
                self.hold = REF_HOLD
            elif self.hold > 0:
                self.hold -= 1
            r = rtc.AudioFrame((rf if self.hold > 0 else self.silent).tobytes(), SR, 1, APM_N)
            self.apm.process_reverse_stream(r)
            f = rtc.AudioFrame(self.mic[:APM_N].tobytes(), SR, 1, APM_N)
            self.apm.process_stream(f)
            out.append(np.frombuffer(bytes(f.data), dtype=np.int16))
            self.mic, self.ref = self.mic[APM_N:], self.ref[APM_N:]
        return np.concatenate(out) if out else np.zeros(0, dtype=np.int16)


class Ear:
    """One Station's stream: the processing, then the cutting (the old ear.py's rules)."""

    def __init__(self):
        self.front = Front()
        self.vad = Vad()
        # the null toward the muted places (ear_null.py): 256 samples late, so the reference and
        # the raw mics wait as long - the canceller needs them in step with the mono
        self.null = ear_null.STATE["null"] = ear_null.Null()
        self.d_raw = np.zeros((ear_null.HOP, 7), np.int16)
        self.d_ref = np.zeros(ear_null.HOP, np.int16)
        self.box = collections.deque(maxlen=int(BOX_S * SR / CHUNK))
        self.box_auto = 0.0
        self.seq = 0
        # samples waiting for a whole VAD chunk: processed mic, its input, raw mics, ref, flags
        self.q_e, self.q_y = np.zeros(0, np.float32), np.zeros(0, np.float32)
        self.q_raw, self.q_ref = np.zeros((0, 7), np.int16), np.zeros(0, np.int16)
        self.pending_raw = collections.deque()
        self.flags = 0
        self.pre = collections.deque(maxlen=PREROLL)
        self.seg, self.seg_ref, self.seg_raw = [], [], []
        self.speaking, self.silence, self.playing_until = False, 0.0, 0.0
        self.n_pre, self.t_start, self.t_speech = 0, 0.0, 0.0
        self.stuck_n = 0
        self.stats_t, self.stats = time.time(), collections.Counter()
        self.sent = 0
        hear.STATION.clock(time.time())      # one clock now: the hub's

    # -- out --
    def ui(self, state):
        hear.EVENTS.publish({"type": "ui", "state": state})

    def started(self, t):
        hear.STATION.started(t)
        hear.EVENTS.publish({"type": "hearing", "on": True, "source": "station"})

    def send(self, seg, raw7, meta):
        self.seq += 1
        meta = dict(meta, seq=self.seq, record=bool(self.flags & 4) or None, now=time.time())
        if not meta.get("window"):
            hear.EVENTS.publish({"type": "hearing", "on": False, "source": "station"})
        if seg is None:
            a, mics = np.zeros(0, np.float32), None
        else:
            # the same quantisation the HTTP path had: int16 mono and mics
            a = (np.clip(seg * 32767, -32767, 32767).astype(np.int16).astype(np.float32) / 32768.0)
            mics = raw7
        hear.STATION.submit(a, mics, meta)
        self.sent += 1

    # -- in --
    def feed(self, flags, raw):
        mics, ref = unpack(raw)
        self.flags = flags
        # the mics -> one mono with the muted places (the TV) taken out; with no muted place it is
        # the plain mean of HEAR_MICS, as before
        mono = self.null.process(mics.astype(np.float64))
        self.d_raw = np.concatenate([self.d_raw, mics])
        self.d_ref = np.concatenate([self.d_ref, ref])
        k = len(mono)
        mics, ref = self.d_raw[:k], self.d_ref[:k]
        self.d_raw, self.d_ref = self.d_raw[k:], self.d_ref[k:]
        e = self.front.process(mono, ref)
        # the raw side, delayed with the processed one (APM holds up to 10 ms)
        self.q_y = np.concatenate([self.q_y, (mono * MIC_GAIN / 32768.0).astype(np.float32)])
        self.q_raw = np.concatenate([self.q_raw, mics])
        self.q_ref = np.concatenate([self.q_ref, ref])
        self.q_e = np.concatenate([self.q_e, (e.astype(np.float32) * MIC_GAIN / 32768.0)])
        while len(self.q_e) >= CHUNK and len(self.q_y) >= CHUNK:
            ce, cy = self.q_e[:CHUNK], self.q_y[:CHUNK]
            craw, cref = self.q_raw[:CHUNK], self.q_ref[:CHUNK]
            self.q_e, self.q_y = self.q_e[CHUNK:], self.q_y[CHUNK:]
            self.q_raw, self.q_ref = self.q_raw[CHUNK:], self.q_ref[CHUNK:]
            self.chunk(ce, cy, craw, cref)

    def chunk(self, e, y, raw, ref):
        now = time.time()
        music, her_flag = bool(self.flags & 1), bool(self.flags & 2)
        x = ref.astype(np.float64) * REF_GAIN / 32768.0
        ref_on = float(np.sqrt(np.mean(x * x))) > REF_ACTIVE
        if ref_on:
            self.playing_until = now + 0.3
        play_now = ref_on or now < self.playing_until or her_flag or music
        # HER voice (not music): what counts as her echo; music counted too once and every
        # command said over it was thrown away as her echo (2026-11-23)
        her_now = her_flag or (now < self.playing_until and not music)
        en_in, en_out = float(np.mean(y.astype(np.float64) ** 2)), float(np.mean(e.astype(np.float64) ** 2))
        st = self.stats
        st["mic_peak"] = max(st["mic_peak"], float(np.abs(raw).max()) / 32768.0)
        if play_now:
            st["aec_in"] += en_in
            st["aec_out"] += en_out
            st["play_n"] += 1
        st["clip"] += int(np.count_nonzero(np.abs(e) >= 1.0))
        e = np.clip(e, -1.0, 1.0)
        p = self.vad(e)
        self.box.append((y.astype(np.float16), x.astype(np.float16), e.astype(np.float16), p,
                         1 * music + 2 * her_now + 4 * ref_on))
        # a stuck VAD: loud in the room, no voice for VAD_STUCK s, not over her own voice. Over
        # music too: the VAD gave 0.00 for whole songs (2026-11-26); music alone scores above
        # 0.01 now and then, so a flat zero is the stuck state
        loud = float(np.abs(raw).max()) / 32768.0 > LOUD
        if loud and p < (0.01 if play_now else 0.05) and not her_now:
            self.stuck_n += 1
            if self.stuck_n * CHUNK / SR >= VAD_STUCK:
                self.vad.reset()
                self.vad.resets += 1
                self.stuck_n = 0
                log("vad: loud but no voice for 3 s - its state reset" + (" (over music)" if play_now else ""))
                if play_now and now - self.box_auto > BOX_AUTO_EVERY:
                    self.box_auto = now
                    self.dump("deaf-music")
        else:
            self.stuck_n = 0
        st["vad_max"] = max(st["vad_max"], p)
        st["chunks"] += 1
        st["speech"] += p > START_P
        st["play"] += now < self.playing_until
        raw_f = raw.astype(np.float32) / 32768.0
        if not self.speaking:
            self.pre.append((e, her_now, en_in, en_out, raw_f))
            if p > START_P:
                self.speaking, self.silence = True, 0.0
                self.seg = [c for c, *_ in self.pre]
                self.seg_ref = [(r, i, o) for _, r, i, o, _ in self.pre]
                self.seg_raw = [m for *_, m in self.pre]
                self.n_pre = len(self.seg)
                self.pre.clear()
                self.t_start = now - len(self.seg) * CHUNK / SR
                self.t_speech = now - CHUNK / SR
                self.started(now)
                if not play_now:
                    self.ui("on")
        else:
            self.seg.append(e)
            self.seg_ref.append((her_now, en_in, en_out))
            self.seg_raw.append(raw_f)
            self.silence = self.silence + CHUNK / SR if p < END_P else 0.0
            dur = len(self.seg) * CHUNK / SR
            # windows only while HER voice plays (its echo keeps the VAD on): a «стоп» over her
            if her_now and dur >= PLAYBACK_WINDOW and self.silence < END_SILENCE:
                meta = {"dur": round(dur, 2), "start": self.t_start, "speech_start": self.t_speech, "end": now,
                        "playback": 1.0, "residual": None, "window": True}
                self.send(np.concatenate(self.seg), None, meta)
                self.seg, self.seg_ref, self.seg_raw = self.seg[-OVERLAP:], self.seg_ref[-OVERLAP:], self.seg_raw[-OVERLAP:]
                self.n_pre = 0
                self.t_start = self.t_speech = now - len(self.seg) * CHUNK / SR
                return
            voiced = (len(self.seg) - self.n_pre) * CHUNK / SR - self.silence
            end_silence = END_SILENCE_SHORT if voiced < SHORT_HEAD and not play_now else END_SILENCE
            paused = self.silence >= end_silence
            if paused or dur >= MAX_SPEECH or (dur >= SOFT_MAX and self.silence >= DIP):
                self.speaking = False
                speech = dur - self.silence
                played = [(i, o) for r, i, o in self.seg_ref if r]
                playback = len(played) / len(self.seg_ref)
                residual = (sum(o for _, o in played) / (sum(i for i, _ in played) + 1e-12)) if played else 1.0
                meta = {"dur": round(speech, 2), "start": self.t_start, "speech_start": self.t_speech, "end": now,
                        "playback": round(playback, 3), "residual": round(residual, 3), "chained": not paused}
                if speech < MIN_SPEECH:
                    self.send(None, None, meta)          # a click: the hub only learns it ended
                    self.ui("none")
                else:
                    keep_n = int(len(self.seg) * CHUNK - max(0.0, self.silence - TAIL_KEEP) * SR)
                    seg = np.concatenate(self.seg)[:keep_n]
                    raw7 = np.clip(np.concatenate(self.seg_raw)[:keep_n] * 32768, -32768, 32767).astype(np.int16)
                    self.send(seg, raw7, meta)
                    if playback < 0.5:
                        self.ui("stt")
        if now - self.stats_t > 60:
            self.minute(now)

    def minute(self, now):
        st = self.stats
        line = (f"level: speech {st['speech']}/{st['chunks']} chunks, playback {st['play']}, vad resets {self.vad.resets}, "
                f"mic peak {20 * np.log10(st['mic_peak'] + 1e-12):.0f} dBFS, vad max {st['vad_max']:.2f}, sent {self.sent}")
        if st["play_n"]:
            line += f", aec out/in {10 * np.log10((st['aec_out'] + 1e-12) / (st['aec_in'] + 1e-12)):+.1f} dB"
        if st["clip"]:
            line += f", clipped {st['clip']}"
        log(line)
        STATE["minute"] = line
        self.stats_t, self.stats, self.sent = now, collections.Counter(), 0

    def dump(self, reason):
        items = list(self.box)
        if not items:
            return None
        os.makedirs(BOX_DIR, exist_ok=True)
        path = os.path.join(BOX_DIR, time.strftime("%Y%m%d-%H%M%S") + f"-{reason}.npz")
        np.savez(path, y=np.concatenate([i[0] for i in items]), x=np.concatenate([i[1] for i in items]),
                 e=np.concatenate([i[2] for i in items]), p=np.array([i[3] for i in items], dtype=np.float32),
                 flags=np.array([i[4] for i in items], dtype=np.uint8), sr=SR, chunk=CHUNK, end=time.time())
        for f in sorted(f for f in os.listdir(BOX_DIR) if f.endswith(".npz"))[:-12]:
            os.remove(os.path.join(BOX_DIR, f))
        log(f"black box: {path} ({len(items) * CHUNK / SR:.0f} s)")
        return path


EAR = {"ear": None}


def _read(conn, n):
    buf = bytearray()
    while len(buf) < n:
        b = conn.recv(n - len(buf))
        if not b:
            raise ConnectionError("closed")
        buf += b
    return bytes(buf)


def _serve(conn, peer):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(10)
    line = b""
    while not line.endswith(b"\n"):
        line += _read(conn, 1)
        if len(line) > 1000:
            raise ValueError("no header")
    h = json.loads(line)
    words, block = int(h.get("words", 5)), int(h.get("block", 256))
    n = 1 + block * words * 4
    ear = EAR["ear"] = Ear()
    STATE.update(connected=True, peer=peer[0], since=time.time())
    log(f"the Station streams from {peer[0]}: {h}")
    while True:
        b = _read(conn, n)
        ear.feed(b[0], b[1:])


def server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(2)
    log(f"waiting for the Station on :{PORT}")
    while True:
        conn, peer = s.accept()
        # one Station: a new connection replaces the old one (it reconnected)
        def run(c=conn, p=peer):
            try:
                _serve(c, p)
            except Exception as e:
                log(f"stream from {p[0]} ended: {e}")
            finally:
                STATE["connected"] = False
                try:
                    c.close()
                except OSError:
                    pass
        threading.Thread(target=run, daemon=True).start()


def null_watch():
    """The «Места» page: the sound null's switch and its places -> the weights in the running ear
    (the saved ones if they fit, else made again from the STT log)."""
    last, ear = None, None
    time.sleep(15)                          # the voice base loads first
    while True:
        try:
            places = [(n, round(a)) for n, a in hear.null_places()]
            null = ear_null.STATE["null"]
            if null is not None and (places != last or null is not ear):
                if not places:
                    ear_null.plain()
                    if last:
                        log("the sound null is off: the plain mono")
                elif not null.load(places):
                    vb = hear.V()
                    family = list(vb.profiles) if vb is not None else []
                    log(f"sound null for {places}: making it")
                    want = list(places)
                    ear_null.recalibrate(places, family,
                                         wanted=lambda: [(n, round(a)) for n, a in hear.null_places()] == want)
                last, ear = places, null
        except Exception as e:
            log("null watch:", e)
        time.sleep(5)


def start():
    threading.Thread(target=server, daemon=True).start()
    threading.Thread(target=null_watch, daemon=True).start()
