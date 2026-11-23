"""Eva's voice: sentences in, speech out, in order, with lip sync on the
screen. stop() cuts everything at once; what she managed to say is reported
to `on_stopped(gen, said)` so the conversation knows where she was cut off.
"""
import os
import queue
import re
import subprocess
import threading
import time

import say
from core.util import log

NOT_SPOKEN = re.compile("[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF\\uFE0F\\u200D]+")
SPEAKING_FLAG = "/run/eva/speaking"   # exists while her voice plays (ear.py reads it)


def ui(event):
    say.send(event)


class Voice:
    def __init__(self):
        self.text_q = queue.Queue()
        self.audio_q = queue.Queue()
        self.gen = 0                      # bumped by stop(): stale work is dropped
        self.play = None
        self.busy_until = 0.0
        self.plays = []                   # [start, end] of each sentence played; end None = playing
        self.played = {}                  # gen -> sentences that actually started playing
        self.recent = []                  # (time, sentence) she said lately: her echo's words
        self.on_stopped = None
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(SPEAKING_FLAG), exist_ok=True)
        self._flag(False)
        threading.Thread(target=self._synth, daemon=True).start()
        threading.Thread(target=self._player, daemon=True).start()

    @staticmethod
    def _flag(on):
        try:
            if on:
                open(SPEAKING_FLAG, "w").close()      # ear.py: recognise in short windows now
            else:
                os.remove(SPEAKING_FLAG)
        except OSError:
            pass

    def speaking(self):
        return self.play is not None or not self.audio_q.empty() or not self.text_q.empty() \
            or time.time() < self.busy_until

    def say(self, sentence, gen=None):
        """Queue a sentence of reply `gen`; a reply that was stopped meanwhile
        (its gen is old) is dropped, its tail never reaches the speaker."""
        if gen is not None and gen != self.gen:
            return False
        sentence = NOT_SPOKEN.sub("", sentence).strip()     # emoji: nothing to say aloud
        if not re.search(r"\w", sentence):
            return False
        self.busy_until = time.time() + 2
        self.recent = [(t, s) for t, s in self.recent if time.time() - t < 60] + [(time.time(), sentence)]
        self.text_q.put((self.gen, sentence))
        return True

    def heard_while_talking(self, speech_start, end):
        """Did a phrase begin while her voice was in the room? By the time its
        text arrives (silence + recognition: up to ~3 s) she may already be
        quiet, so the phrase's own capture time decides, not the arrival time."""
        now = time.time()
        return any(speech_start <= (e if e is not None else now) + 0.25 and end >= s for s, e in self.plays)

    def own_words(self):
        # ё -> е: she says "Всё отлично", the recogniser writes "все" - her own
        # "все" was taken for a stop word and cut her off (2026-11-19)
        return set(re.findall(r"\w+", " ".join(s for _, s in self.recent).lower().replace("ё", "е")))

    def is_echo(self, text):
        """A phrase heard while she talks, made of her own current words = her echo."""
        words = re.findall(r"\w+", text.lower().replace("ё", "е"))
        own = self.own_words()
        return bool(words) and sum(w in own for w in words) / len(words) >= 0.7

    def stop(self):
        with self.lock:
            stopped = self.gen
            self.gen += 1
            for q in (self.text_q, self.audio_q):
                while not q.empty():
                    q.get_nowait()
            if self.play:
                self.play.kill()
        self.busy_until = 0
        self._flag(False)
        ui({"type": "stop"})
        if self.on_stopped:
            self.on_stopped(stopped, list(self.played.get(stopped, [])))

    def _synth(self):
        while True:
            gen, s = self.text_q.get()
            if gen != self.gen:
                continue
            try:
                wav = say.tts(s)
                track = say.visemes(wav)
            except Exception as e:  # network, TTS service
                log("tts error:", e)
                continue
            if gen == self.gen:
                self.audio_q.put((gen, s, wav, track))

    def _player(self):
        while True:
            gen, s, wav, track = self.audio_q.get()
            if gen != self.gen:
                continue
            start = time.time() + 0.06
            ui({"type": "speak", "start": start * 1000 + say.latency_ms(), "hop": say.HOP * 1000,
                "frames": track, "text": s})
            while time.time() < start:
                time.sleep(0.002)
            with self.lock:
                if gen != self.gen:
                    continue
                span = [time.time(), None]
                self.played = {g: v for g, v in self.played.items() if g >= gen - 5}
                self.played.setdefault(gen, []).append(s)
                self._flag(True)
                self.plays = [p for p in self.plays if p[1] is None or time.time() - p[1] < 120] + [span]
                self.play = subprocess.Popen(["aplay", "-q", "-D", say.PLAY_DEV,
                                              f"--buffer-time={say.BUFFER_US}", wav])
            self.play.wait()
            span[1] = time.time()
            self.play = None
            if self.audio_q.empty():
                self._flag(False)
            self.busy_until = time.time() + 0.4      # the room's echo dies down


VOLUME_FILE = "/var/lib/eva/volume"      # percent, kept over restarts
VOL_DB = (-60.0, -15.0)                  # 0 % .. 100 % of the speakers' master (0.125 dB a step, 1023 = +24 dB)
VOL_STEP = 0.5                           # % per click of the ring (4 was far too sensitive, 1 still a bit)
VOL = {"pct": None, "want": None, "show": False, "mixer": None}
VOL_LOCK = threading.Condition()


def volume_get():
    if VOL["want"] is not None:
        return int(round(VOL["want"]))
    try:
        return max(0, min(100, int(round(float(open(VOLUME_FILE).read().strip())))))
    except (OSError, ValueError):
        return 55                        # -35 dB: the level station-audio-init.sh sets


def _mixer_write(pct):
    """Both amplifiers through one amixer kept open (-s: commands on stdin) -
    a fork per click was most of the ring's lag."""
    db = VOL_DB[0] + (VOL_DB[1] - VOL_DB[0]) * pct / 100
    raw = 0 if pct <= 0 else int(round(1023 - (24.0 - db) / 0.125))
    cmd = "".join(f'cset "name={p} Master Playback Volume" {raw}\n' for p in ("Subwoofer", "Tweeters"))
    for _ in range(2):
        m = VOL["mixer"]
        if m is None or m.poll() is not None:
            m = VOL["mixer"] = subprocess.Popen(["amixer", "-q", "-c", "snddaudio1", "-s"],
                                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, text=True)
        try:
            m.stdin.write(cmd)
            m.stdin.flush()
            return
        except OSError:
            VOL["mixer"] = None


def _volume_apply():
    """Applies the latest level asked for: clicks that come faster than the
    mixer and the screen take are merged, not queued (the ring lagged behind
    the hand by seconds, 2026-11-21)."""
    while True:
        with VOL_LOCK:
            while VOL["want"] is None or VOL["want"] == VOL["pct"]:
                VOL_LOCK.wait()
            pct, show = VOL["want"], VOL["show"]
            VOL["pct"], VOL["show"] = pct, False
        _mixer_write(pct)
        if show:
            ui({"type": "volume", "level": round(pct / 100, 4)})    # the LED ring: an arc from blue to red
        try:
            with open(VOLUME_FILE, "w") as f:
                f.write(f"{pct:g}")
        except OSError:
            pass


threading.Thread(target=_volume_apply, daemon=True).start()


def volume_set(pct, show=True):
    """The speakers' master (both amplifiers) - her voice and the music alike.
    Returns at once; the level is applied by _volume_apply."""
    pct = max(0.0, min(100.0, round(float(pct) * 2) / 2))
    with VOL_LOCK:
        VOL["want"] = pct
        VOL["show"] = VOL["show"] or show
        if VOL["pct"] == pct and show:
            VOL["pct"] = None                # same level: show it anyway
        VOL_LOCK.notify()
    return pct


def volume_ring():
    """The rotary ring on top of the Station (input "rotary-encoder", relative
    steps): it was not read at all - the ring did nothing (2026-11-20)."""
    dev = None
    for e in sorted(os.listdir("/sys/class/input")):
        try:
            if open(f"/sys/class/input/{e}/device/name").read().strip() == "rotary-encoder":
                dev = "/dev/input/" + e
        except OSError:
            pass
    if not dev:
        log("no rotary-encoder device")
        return
    try:
        pct = float(open(VOLUME_FILE).read().strip())
    except (OSError, ValueError):
        pct = 55.0
    pct = volume_set(pct, show=False)
    with open(dev, "rb") as f:
        while True:
            ev = f.read(24)
            etype, value = int.from_bytes(ev[16:18], "little"), int.from_bytes(ev[20:24], "little", signed=True)
            if etype == 2 and value:                       # EV_REL: one click
                pct = volume_set(pct + VOL_STEP * value)


def buttons(voice):
    """Any Station key stops her (gpio-keys: the Alice key, the mic key, volume)."""
    dev = None
    for e in sorted(os.listdir("/sys/class/input")):
        try:
            if open(f"/sys/class/input/{e}/device/name").read().strip() == "gpio-keys":
                dev = "/dev/input/" + e
        except OSError:
            pass
    if not dev:
        log("no gpio-keys device")
        return
    with open(dev, "rb") as f:
        while True:
            ev = f.read(24)
            etype, code, value = (int.from_bytes(ev[16:18], "little"), int.from_bytes(ev[18:20], "little"),
                                  int.from_bytes(ev[20:24], "little", signed=True))
            if etype == 1 and value == 1 and voice.speaking():
                log(f"key {code}: stop")
                voice.stop()
