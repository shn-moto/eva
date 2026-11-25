"""Eva's voice: sentences in, speech out, in order. The sound is made here
(TTS) and played by the body on the Station (app/body.py: lips, the speaker,
the "speaking" flag for the ear); its events say when each sentence really
started and ended. stop() cuts everything at once; what she managed to say is
reported to `on_stopped(gen, said)` so the conversation knows where she was cut off.
"""
import itertools
import queue
import re
import threading
import time

import say
from core import body
from core.util import log

NOT_SPOKEN = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")
_ids = itertools.count(int(time.time()))


def ui(event):
    if event.get("type") == "chat" and event.get("text"):
        event = dict(event, text=say.untag(event["text"]))      # the TV shows words, not [laughs]
    if event.get("type") == "avatar" and event.get("mode"):
        _avatar_changed(event["mode"])
    body.ui(event)


def _avatar_changed(mode):
    """Her voice follows her look (say.voice_for): the new voice's short phrases
    («секунду, посмотрю») are made ahead, so the first one after the change is not late."""
    old = say.voice_for()
    say.set_avatar(mode)
    if say.voice_for() != old:
        log(f"voice: {say.voice_for()} for the avatar {mode}")
        try:
            import dialog
            fillers = list(dialog.FILLERS)
        except Exception:
            fillers = []
        threading.Thread(target=lambda: [say.tts(f) for f in fillers], daemon=True).start()


class Voice:
    def __init__(self):
        self.text_q = queue.Queue()
        self.gen = 0                      # bumped by stop(): stale work is dropped
        self.play = None                  # the sentence the body plays now (its id)
        self.pending = set()              # sent to the body, not ended yet
        self.busy_until = 0.0
        self.plays = []                   # [start, end] of each sentence played (the body's clock); end None = playing
        self.played = {}                  # gen -> sentences that actually started playing
        self.recent = []                  # (time, sentence) she said lately: her echo's words
        self.on_stopped = None
        self.lock = threading.Lock()
        self._spans = {}                  # id -> its [start, end] in plays
        self._first = set()               # replies whose first sound has played
        self.on_first = None              # (gen, t): the first sound of a reply (the reaction time)
        body.on("speech", self._event)
        threading.Thread(target=self._synth, daemon=True).start()

    def speaking(self):
        return self.play is not None or bool(self.pending) or not self.text_q.empty()             or time.time() < self.busy_until

    def say(self, sentence, gen=None):
        """Queue a sentence of reply `gen`; a reply that was stopped meanwhile
        (its gen is old) is dropped, its tail never reaches the speaker."""
        if gen is not None and gen != self.gen:
            return False
        sentence = NOT_SPOKEN.sub("", sentence).strip()     # emoji: nothing to say aloud
        if not re.search(r"\w", say.untag(sentence)):
            return False
        self.busy_until = time.time() + 2
        # her echo is matched by the words she says, not by the emotion tags she performs
        self.recent = [(t, s) for t, s in self.recent if time.time() - t < 60] + [(time.time(), say.untag(sentence))]
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
            while not self.text_q.empty():
                self.text_q.get_nowait()
            self.pending.clear()
            new = self.gen
        body.post("/speech/stop", {"gen": new}, timeout=2)
        self.play = None
        self.busy_until = 0
        ui({"type": "stop"})
        if self.on_stopped:
            self.on_stopped(stopped, list(self.played.get(stopped, [])))

    def _synth(self):
        """TTS in order; each sentence goes to the body as soon as it is made."""
        while True:
            gen, s = self.text_q.get()
            if gen != self.gen:
                continue
            try:
                wav = say.tts(s)
            except Exception as e:  # network, TTS service
                log("tts error:", e)
                continue
            if gen != self.gen:
                continue
            sid = str(next(_ids))
            self.pending.add(sid)
            try:
                frames = say.visemes(wav)          # the lips, from the sound itself
            except Exception as e:
                log("visemes:", e)
                frames = []
            r = body.speech(gen, sid, s, wav, frames)
            if not (r or {}).get("queued"):
                self.pending.discard(sid)

    def _event(self, ev):
        sid, gen = ev.get("id"), ev.get("gen")
        if ev.get("state") == "start":
            with self.lock:
                span = [ev["t"], None]
                self._spans[sid] = span
                self.plays = [p for p in self.plays if p[1] is None or time.time() - p[1] < 120] + [span]
                self.played = {g: v for g, v in self.played.items() if g >= gen - 5}
                self.played.setdefault(gen, []).append(ev.get("text", ""))
                self.play = sid
                first = gen not in self._first
                self._first.add(gen)
            if first and self.on_first:
                self.on_first(gen, ev["t"])
        elif ev.get("state") == "end":
            with self.lock:
                span = self._spans.pop(sid, None)
                if span:
                    span[1] = ev["t"]
                if self.play == sid:
                    self.play = None
                self.pending.discard(sid)
            self.busy_until = time.time() + 0.4      # the room's echo dies down


# ---- the speakers' master: kept by the body (app/body.py) ----
VOL = {"pct": None}


def _volume_event(ev):
    VOL["pct"] = ev.get("pct")


body.on("volume", _volume_event)


def volume_get():
    if VOL["pct"] is None:
        r = body.get("/volume")
        if r:
            VOL["pct"] = r.get("pct")
    return int(round(VOL["pct"] if VOL["pct"] is not None else 55))


def volume_set(pct, show=True):
    """The speakers' master (both amplifiers) - her voice and the music alike."""
    pct = max(0.0, min(100.0, round(float(pct) * 2) / 2))
    VOL["pct"] = pct
    threading.Thread(target=body.post, args=("/volume", {"pct": pct, "show": show}), daemon=True).start()
    return pct


def volume_ring():
    """The ring is read by the body; its turns come here as "volume" events."""


def buttons(voice):
    """A Station key: the body has cut her sound already; the reply goes too."""
    def key(ev):
        if voice.speaking():
            log(f"key {ev.get('code')}: stop")
            voice.stop()
    body.on("key", key)
