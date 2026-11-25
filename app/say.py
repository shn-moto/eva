#!/usr/bin/env python3
"""Say a phrase with Eva's voice and move her lips with it.

    eva_say.py "Привет! Я Ева."            speak (TTS -> lips -> speaker)
    eva_say.py --calibrate                 measure the speaker's output latency

Lip sync: the TTS audio is analysed *before* playback into one viseme and one
mouth opening per 20 ms (loudness -> opening, spectrum -> vowel shape). The
page gets the whole track with the wall-clock moment the first sample leaves
the speaker, and plays it on the same clock, so lips and sound share one
timeline. The speaker's own delay (ALSA buffer + device start) is measured by
--calibrate through the amplifier tap on the mic board (ADC3 right).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import wave

import numpy as np
from core import profile

SECRETS = os.environ.get("EVA_SECRETS", "/etc/eva/secrets.env")
CACHE = "/var/cache/eva/tts"
STATE = "/var/lib/eva/lipsync.json"           # {"latency_ms": ...} from --calibrate
UI = "http://127.0.0.1:8080/api/event"
PLAY_DEV = "eva"                              # the dmix mixer (/etc/asound.conf): music can play under her voice
BUFFER_US = 80000                             # small, fixed ALSA buffer: a known delay
HOP = 0.02                                    # one viseme per 20 ms


def env():
    e = {}
    for line in open(SECRETS, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            e[k.strip()] = v.strip()
    return e


# Her voice follows her look on the TV (user, 2026-11-25): profile.json "voices" maps an
# avatar (ascii | image | eq | live2d) to a voice; "default" for the rest.
#     "voices": {"default": {"engine": "xai", "voice": "eve"},
#                "live2d":  {"engine": "elevenlabs", "voice": "<voice id>", "model": "eleven_v4_turbo"}}
# xAI's voice id defaults to XAI_VOICE in secrets.env; ElevenLabs needs ELEVENLABS_API_KEY.
# emotion tags she puts into her spoken text ([laughs], [whispers]...): performed by
# ElevenLabs (checked 2026-11-25 on v4, v4 Turbo, v3 Conversational: not read aloud),
# cut out for xAI and everywhere the text is shown or kept
TAGS = re.compile(r"\s*\[[a-z][a-z ]{1,30}\]\s*")


def untag(text):
    return re.sub(r"\s{2,}", " ", TAGS.sub(" ", text or "")).strip()


AVATAR_FILE = "/var/lib/eva/avatar"           # the brain's copy of what the TV shows (core/speech.ui keeps it)
_avatar = {"mode": None}


def avatar():
    if _avatar["mode"] is None:
        try:
            _avatar["mode"] = open(AVATAR_FILE, encoding="utf-8").read().strip() or "ascii"
        except OSError:
            _avatar["mode"] = "ascii"
    return _avatar["mode"]


def set_avatar(mode):
    _avatar["mode"] = mode
    try:
        with open(AVATAR_FILE, "w", encoding="utf-8") as f:
            f.write(mode)
    except OSError:
        pass


def voice_for(mode=None):
    """The voice spec of an avatar: {"engine", "voice", "model"}."""
    v = profile.VOICES.get(mode or avatar()) or profile.VOICES.get("default") or {"engine": "xai"}
    return dict(v)


def _xai(text, voice, e):
    req = urllib.request.Request(
        "https://api.x.ai/v1/tts",
        data=json.dumps({"text": text, "voice_id": voice, "language": "ru"}).encode(),
        headers={"Authorization": "Bearer " + e["XAI_API_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


def _elevenlabs(text, voice, model, e):
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": model or "eleven_v4_turbo", "language_code": "ru"}).encode(),
        headers={"xi-api-key": e["ELEVENLABS_API_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


def tts(text, spec=None):
    """mp3 from the avatar's voice (xAI or ElevenLabs), cached, decoded to mono int16 wav.
    ElevenLabs away or failing: the phrase in xAI's voice - she never goes silent."""
    e = env()
    spec = spec or voice_for()
    engine = spec.get("engine", "xai")
    voice = spec.get("voice") or (e.get("XAI_VOICE", "eve") if engine == "xai" else "")
    model = spec.get("model", "")
    tag = f"{voice}|{text}" if engine == "xai" else f"{engine}|{voice}|{model}|{text}"   # old xAI cache stays valid
    key = hashlib.sha1(tag.encode()).hexdigest()[:20]
    os.makedirs(CACHE, exist_ok=True)
    mp3, wav = os.path.join(CACHE, key + ".mp3"), os.path.join(CACHE, key + ".wav")
    if os.path.exists(wav):
        try:
            os.utime(wav)                 # used: the newest in the cache, pruned last
        except OSError:
            pass
        return wav
    if engine == "elevenlabs":
        try:
            data = _elevenlabs(text, voice, model, e)
        except Exception as ex:
            print(f"tts: elevenlabs failed ({ex}) - xAI this time", file=sys.stderr, flush=True)
            return tts(untag(text), {"engine": "xai"})
    else:
        data = _xai(untag(text), voice, e)
    # written under a temporary name and renamed: a power cut mid-write
    # must not leave a truncated phrase in the cache for good
    tmp_mp3, tmp_wav = mp3 + ".part", wav + ".part.wav"
    with open(tmp_mp3, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    subprocess.run(["mpg123", "-q", "-m", "-w", tmp_wav, tmp_mp3], check=True)
    with open(tmp_wav, "rb") as f:
        os.fsync(f.fileno())
    os.remove(tmp_mp3)                    # the wav is what is played; the mp3 doubled the cache
    os.replace(tmp_wav, wav)
    prune()
    return wav


CACHE_MAX = profile.tune("say.tts_cache_bytes", 40 * 1024 * 1024)      # bytes: every phrase she ever said was kept - 174 MB of a 2 GB root (2026-11-23)


def prune():
    """Least recently used phrases go once the cache is over CACHE_MAX (the
    fillers and fixed replies are used often and stay)."""
    try:
        files = [os.path.join(CACHE, n) for n in os.listdir(CACHE)]
        st = sorted(((os.stat(p), p) for p in files if os.path.isfile(p)), key=lambda x: x[0].st_mtime)
    except OSError:
        return
    total = sum(s.st_size for s, _ in st)
    if total <= CACHE_MAX:
        return
    for s, p in st:                       # down to 80 %: not one file at every new phrase
        if total <= CACHE_MAX * 0.8:
            break
        try:
            os.remove(p)
            total -= s.st_size
        except OSError:
            pass


def visemes(wav):
    """[(viseme, opening 0..1)] per HOP from the audio itself."""
    with wave.open(wav) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    hop, win = int(sr * HOP), int(sr * HOP * 2)
    window = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1 / sr)
    low = (freqs >= 200) & (freqs < 900)       # O/U: energy sits low
    mid = (freqs >= 900) & (freqs < 2200)      # A: first formants high, open jaw
    high = (freqs >= 2200) & (freqs < 7000)    # I/E and sibilants: high formants
    frames = []
    for i in range(0, max(1, len(x) - win), hop):
        f = x[i:i + win]
        if len(f) < win:
            f = np.pad(f, (0, win - len(f)))
        rms = float(np.sqrt(np.mean(f * f)) + 1e-9)
        s = np.abs(np.fft.rfft(f * window)) ** 2
        el, em, eh = float(s[low].sum()), float(s[mid].sum()), float(s[high].sum())
        frames.append((20 * np.log10(rms), el, em, eh))
    if not frames:
        return []
    peak = max(f[0] for f in frames)
    out, opening = [], 0.0
    for db, el, em, eh in frames:
        target = min(1.0, max(0.0, (db - (peak - 32)) / 26))   # 32 dB below the peak = closed
        # jaw: opens fast, closes a little slower (natural, hides frame jitter)
        opening += (target - opening) * (0.75 if target > opening else 0.45)
        tot = el + em + eh + 1e-12
        if opening < 0.08:
            v = "rest"
        elif eh / tot > 0.45:
            v = "ee" if opening > 0.45 else "ih"
        elif el / tot > 0.62:
            v = "ou" if opening < 0.5 else "oh"
        else:
            v = "aa"
        out.append([v, round(opening, 3)])
    # a vowel shape held for less than 2 frames is noise: take the neighbour's
    for i in range(1, len(out) - 1):
        if out[i][0] != out[i - 1][0] and out[i - 1][0] == out[i + 1][0]:
            out[i][0] = out[i - 1][0]
    return out


def send(event):
    req = urllib.request.Request(UI, data=json.dumps(event, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError as e:
        print("ui not reachable:", e, file=sys.stderr)


def latency_ms():
    try:
        return float(json.load(open(STATE))["latency_ms"])
    except (OSError, ValueError, KeyError):
        return 150.0


def say(text):
    t0 = time.time()
    wav = tts(text)
    track = visemes(wav)
    t1 = time.time()
    send({"type": "chat", "who": "eva", "text": text})
    # the page gets the track a little ahead; the first sample leaves the
    # speaker at `start` (wall clock of this device, ms)
    lead = 0.12
    start_wall = time.time() + lead
    send({"type": "speak", "start": (start_wall * 1000) + latency_ms(), "hop": HOP * 1000,
          "frames": track, "text": text})
    while time.time() < start_wall:
        time.sleep(0.002)
    subprocess.run(["aplay", "-q", "-D", PLAY_DEV, f"--buffer-time={BUFFER_US}", wav])
    print(f"tts+analysis {1000 * (t1 - t0):.0f} ms, {len(track)} frames, latency {latency_ms():.0f} ms")


def calibrate():
    """Play a click, record the amplifier tap (ADC3 right = capture channel 3),
    find when the click shows up there relative to the aplay start."""
    sr = 24000
    click = np.zeros(int(sr * 0.6), dtype=np.int16)
    click[int(sr * 0.05):int(sr * 0.05) + 240] = (8000 * np.sin(np.linspace(0, 60 * np.pi, 240))).astype(np.int16)
    path = "/tmp/eva-click.wav"
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(click.tobytes())
    import threading
    results = []
    PERIOD = 160                                   # frames per capture period: 10 ms
    for _ in range(5):
        rec = subprocess.Popen(["arecord", "-q", "-D", "hw:snddaudio0,0", "-c", "5", "-f", "S32_LE",
                                "-r", "16000", f"--period-size={PERIOD}", "-t", "raw"],
                               stdout=subprocess.PIPE, bufsize=0)
        chunk = PERIOD * 5 * 4
        first = rec.stdout.read(chunk)             # arrives one period after capture began
        t_first = time.time()
        parts = [first]
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                b = rec.stdout.read(chunk)
                if not b:
                    break
                parts.append(b)
        th = threading.Thread(target=reader)
        th.start()
        time.sleep(0.3)
        t_play = time.time()
        subprocess.run(["aplay", "-q", "-D", PLAY_DEV, f"--buffer-time={BUFFER_US}", path])
        time.sleep(0.2)
        stop.set()
        rec.terminate()
        th.join()
        raw = b"".join(parts)
        raw = raw[:len(raw) // 20 * 20]
        a = np.frombuffer(raw, dtype=np.int32).reshape(-1, 5).astype(np.float64)
        t_rec0 = t_first - PERIOD / 16000          # wall time of sample 0
        best = None
        for ch in range(5):                        # the tap is the channel with the click
            env_ = np.abs(a[:, ch] - np.median(a[:, ch]))
            base = np.percentile(env_, 50) + 1e-9
            peak = env_.max()
            if peak / base > 30 and (best is None or peak / base > best[1]):
                best = (ch, peak / base, int(np.argmax(env_ > peak * 0.3)))
        if best is None:
            continue
        ch, _, idx = best
        onset_wall = t_rec0 + idx / 16000
        # delay from starting aplay to the click on the amplifier (it is 50 ms into the file)
        results.append((onset_wall - t_play) * 1000 - 50)
        print(f"  channel {ch}: click {results[-1]:.1f} ms after aplay start")
    if not results:
        sys.exit("no click found in any capture channel")
    lat = float(np.median(results))
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump({"latency_ms": round(lat, 1), "samples": [round(r, 1) for r in results]}, open(STATE, "w"))
    print("latency ms:", [round(r, 1) for r in results], "-> median", round(lat, 1))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate":
        calibrate()
    else:
        say(" ".join(sys.argv[1:]) or "Привет! Я Ева. Давай проверим, совпадают ли губы со звуком.")
