"""The ear's decisions, on the hub (docs/EAR-PLAN.md, stage 2, 2026-11-24).

The Station only hears: it cuts the sound into fragments by pauses and length
and sends each one here (mono + its 7 raw mics + the timeline). Here, for each
fragment: echo or not, the text, the direction, the cut where another person
took over (and whether to believe it - the voice base is here), who said each
part, and the sentence a piece belongs to (the Joiner). The finished
utterances go back to the Station on an event stream, with the same fields
the brain got from the Station's ear before, plus who said it.

Ported from app/ear.py (recognise, deliver, accepted_parts, Joiner) with its
numbers. A rule tried for believing a voice cut - each part
prefers its own voice - measured, and off (RELATIVE): see there.
"""
import json
import os
import queue
import re
import threading
import time

import numpy as np

SR = 16000
MIN_SPEECH = 0.35                 # s of speech: less is a click, a cough
SPK_SECONDS = 3.0                 # the first seconds are enough to know a voice
SPLIT_MIN = 3.0                   # s: shorter fragments are one speaker
SPLIT_SURE = 0.3                  # parts this unlike: two people (0 false splits of 25 single phrases)
ECHO_PLAYBACK, ECHO_RESIDUAL = 0.5, 0.1    # under her voice and the canceller left < 10 %: her echo
REL_LEAD, REL_MIN = 0.05, 0.4     # a voice cut believed: each part prefers its own voice by this
# Off: on the bench's clean glues it kept 23 of 36 cuts instead of 18, but on
# the real log (replay 2026-11-24, 196 clips) it split 47 phrases the Station
# kept whole - many wrong: sisters taken for two people, 0.4-1.5 s parts.
# The move to the hub goes with the Station's rule; a better one only once it
# is measured on the phrases a person marked.
RELATIVE = os.environ.get("EVA_HEAR_RELATIVE") == "1"
GLUE_MAX = 30.0                   # s: a held piece and its rest glued at most this long
SHOW_PLAYBACK = 0.3               # heard over her voice / the music: not shown under the face


def S():
    """The STT server module (its recogniser, direction, split, log)."""
    import stt_server
    return stt_server


def V():
    return S().VOICES if S().VOICES is not None and S().VOICES.loaded else None


def log(*a):
    print(time.strftime("%H:%M:%S"), "hear:", *a, flush=True)


# ---- names in the text (the brain sends the household's fixes: its profile is private) ----
EVA_START = re.compile(r"^(?:ево|евро|ега|иво|ива|яро|е вот)\b\s*")
EVA_GLUED = re.compile(r"^(?:ево|ега|яро)(стоп|хватит)")
EVA_STOP = re.compile(r"^его\s+(стоп|хватит|подожди|погоди)\b")
FIXES = []                        # [(compiled regex, name)]: POST /hear/config {"stt_fixes"}


def fix(text):
    """app/names.py fix(): "Ева" at the start, the family's mangled names."""
    t = text.lower().replace("ё", "е").strip()
    if not re.match(r"^(?:ева|эва)\b", t):
        m = EVA_START.match(t)
        if m and t[m.end():]:
            t = "ева " + t[m.end():]
        else:
            m = EVA_GLUED.match(t)
            if m:
                t = "ева " + m.group(1) + t[m.end():]
            else:
                m = EVA_STOP.match(t)
                if m:
                    t = "ева " + t[4:]
    for rx, name in FIXES:
        t = rx.sub(name, t)
    return t


# words a finished sentence does not end with: "Ева," / "погода в" / "скажи что"
OPEN_END = set("""ева эва и а но или в во на с со к ко у о об по за из от до для при про без над под
что чтобы как когда если потому поэтому который которая которые это этот эта мне меня тебе тебя
мой моя наш наша ты я он она мы вы они не ну вот слушай скажи покажи найди""".split())


def incomplete(text):
    words = text.lower().split()
    return bool(words) and words[-1] in OPEN_END


# ---- the event stream to the Station ----
class Events:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, ev):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


EVENTS = Events()


# ---- believing a cut ----
def _sim(e, k, v):
    return max((float(np.asarray(e, np.float32) @ np.asarray(p, np.float32)) for p in v.profiles.get(k, [])), default=0.0)


def cut_believed(left, right, whole):
    """A voice cut: the parts very unlike, or two different family voices, or
    each part prefers its own voice over the other's (REL_LEAD)."""
    if whole < SPLIT_SURE:
        return True
    v = V()
    if v is None:
        return False
    a, b = v.who(left), v.who(right)
    if a is not None and b is not None and a != b:
        return True
    if not RELATIVE:
        return False
    rl, rr = v.ranked(left), v.ranked(right)
    if not rl or not rr:
        return False
    kl, kr = rl[0][1], rr[0][1]
    return (kl != kr and rl[0][0] >= REL_MIN and rr[0][0] >= REL_MIN
            and rl[0][0] - _sim(left, kr, v) >= REL_LEAD and rr[0][0] - _sim(right, kl, v) >= REL_LEAD)


def accepted_parts(parts):
    """The parts with the cuts believed: a change of place always, a change of
    voice by cut_believed. A cut not believed joins its two parts back."""
    if not parts:
        return []
    out = [dict(parts[0])]
    for p in parts[1:]:
        cut = p.get("cut") or {}
        ok = cut.get("by") == "direction" or (
            cut.get("by") == "voice" and cut_believed(out[-1]["emb"], p["emb"], cut.get("whole", 1.0)))
        if ok:
            out.append(dict(p))
        else:
            prev = out[-1]
            longer = prev if prev["to"] - prev["from"] >= p["to"] - p["from"] else p
            prev.update(to=p["to"], text=(prev["text"] + " " + p["text"]).strip(), emb=longer["emb"])
            prev.pop("lags", None)
            prev.pop("az", None)
    return out


# ---- one listening place (the Station; a replay is another, dry one) ----
class Hearing:
    JOIN_GAP = 1.0                # s after the piece ended (on top of the pause that ended it)
    REST_WAIT = 12.0              # s more for a rest that began: then the piece goes alone
    SAME_VOICE = 0.5              # CAM++ similarity of the two pieces to glue them
    AZ_OTHER = 30.0               # degrees apart: another place, not glued
    AZ_SAME = 12.0                # this close: the same place - a voice on the fence is glued

    def __init__(self, name, dry=False):
        self.name, self.dry = name, dry
        self.lock = threading.Lock()
        self.pending = None       # [audio, mics, meta, deadline (hub clock), joining]
        self.last_end = 0.0       # Station clock: when the last fragment ended
        self.cont = 0.0           # Station clock: when a fragment started soon after it
        self.offset = 0.0         # hub clock - Station clock
        self.out = []             # dry: what was delivered
        self.q = None             # the fragments waiting their turn (submit)

    def emit(self, ev):
        ev["source"] = self.name
        if self.dry:
            self.out.append(ev)
        else:
            EVENTS.publish(ev)

    def ui(self, state, text=None):
        self.emit({"type": "ui", "state": state, "text": text})

    def utterance(self, item):
        self.emit({"type": "utterance", "item": {k: v for k, v in item.items() if not k.startswith("_")}})

    def clock(self, station_now):
        if station_now:
            self.offset = time.time() - float(station_now)

    # -- the Joiner (app/ear.py): sentences, not pieces --
    def started(self, t):
        """A fragment began (Station clock): it continues a held piece if in time."""
        with self.lock:
            if t - self.last_end <= self.JOIN_GAP:
                self.cont = t
            if self.pending and t + self.offset <= self.pending[3]:
                self.pending[4] = True

    def hold(self, seg, mics, meta):
        with self.lock:
            if self.last_end > meta["end"]:                # the rest is already over: too late to glue
                p = None
            else:
                joining = meta["end"] < self.cont <= meta["end"] + self.JOIN_GAP
                p = self.pending = [seg, mics, meta, meta["end"] + self.offset + self.JOIN_GAP, joining]
        if p is None:
            self.utterance(meta)
            return
        log(f"held (open end): {meta['text']!r}{' - its rest is coming' if p[4] else ''}")
        self.ui("held", meta["text"])
        threading.Thread(target=self._expire, args=(p,), daemon=True).start()

    def _expire(self, p):
        time.sleep(max(0.0, p[3] - time.time()))
        with self.lock:
            if self.pending is not p:
                return
            if not p[4]:
                self.pending = None
        if p[4]:
            # its "rest" began but may never come (a window over her voice, an echo):
            # the piece is never lost, it goes on its own
            time.sleep(self.REST_WAIT)
            with self.lock:
                if self.pending is not p:
                    return
                self.pending = None
            log(f"held piece sent alone (its rest never came): {p[2]['text']!r}")
        self.utterance(p[2])

    def merge(self):
        """A fragment ended: the held piece it continues, if any (taken)."""
        with self.lock:
            p, self.pending = self.pending, None
            if p and not p[4]:
                self.pending, p = p, None      # not a continuation: back for _expire
        return p

    def release(self):
        """The continuation turned out to be nothing (echo, a cough): send the piece."""
        with self.lock:
            p = self.pending
            if p and p[4]:
                self.pending = None
            else:
                p = None
        if p:
            self.utterance(p[2])

    def glue(self, held, seg, mics, meta, v, rest_az):
        """One sentence of one voice from one place -> (seg, mics, meta) glued;
        another voice or place -> the piece goes alone first."""
        hseg, hmics, hm = held[0], held[1], held[2]
        hv = hm.get("voice")
        unit = lambda x: np.asarray(x, dtype=np.float32) / (np.linalg.norm(x) + 1e-9)
        sim = float(unit(hv) @ unit(v)) if hv is not None and v is not None else 0.0
        a1 = (hm.get("doa") or {}).get("az")
        apart = abs((a1 - rest_az + 180) % 360 - 180) if a1 is not None and rest_az is not None else None
        place = f", {apart:.0f}° apart" if apart is not None else ""
        if apart is not None and apart >= self.AZ_OTHER:
            log(f"not joined: another place{place} (voice {sim:.2f}) after {hm['text']!r}")
            self.utterance(hm)
            return seg, mics, meta
        same_place = apart is not None and apart <= self.AZ_SAME
        if sim < self.SAME_VOICE and not (same_place and sim >= self.SAME_VOICE - 0.15):
            log(f"not joined: another voice ({sim:.2f}{place}) after {hm['text']!r}")
            self.utterance(hm)
            return seg, mics, meta
        gap = np.zeros(int(0.15 * SR), dtype=np.float32)
        m = dict(meta, start=hm["start"], speech_start=hm["speech_start"], dur=round(hm["dur"] + meta["dur"], 2),
                 joined=True, _joined_from=hm.get("audio"))
        # the glued sound's raw mics: both pieces' (the gap as silence), or none -
        # the rest's alone would put the direction of half the phrase on all of it
        if hmics is not None and mics is not None and hmics.shape[1] == mics.shape[1]:
            gm = np.concatenate([hmics, np.zeros((len(gap), hmics.shape[1]), dtype=hmics.dtype), mics])
        else:
            gm = None
        log(f"joined with {hm['text']!r} (same voice {sim:.2f}{place})")
        return np.concatenate([hseg, gap, seg]), gm, m

    # -- a fragment --
    def submit(self, seg, mics, meta):
        """A fragment from the Station: in the order it came. One after another,
        not in parallel threads: a short fragment overtook the long one before it
        (the utterances came out of order) and a piece still being recognised
        could not be glued with its rest. Windows over her voice go at once,
        apart - a «стоп» must not wait."""
        if meta.get("window"):
            threading.Thread(target=self.fragment, args=(seg, mics, meta), daemon=True).start()
            return
        if self.q is None:
            self.q = queue.Queue()
            threading.Thread(target=self._worker, daemon=True).start()
        self.q.put((seg, mics, meta))

    def _worker(self):
        while True:
            seg, mics, meta = self.q.get()
            try:
                self.fragment(seg, mics, meta)
            except Exception as e:          # one bad fragment must not stop the ear
                log(f"fragment #{meta.get('seq')} failed: {e!r}")
                self.ui("none")

    def fragment(self, seg, mics, meta):
        """seg: float mono (the canceller's output), mics: int16 samples x 7 or
        None, meta: the Station's timeline and echo measures."""
        t = time.time()
        self.clock(meta.get("now"))
        window = bool(meta.get("window"))
        if meta.get("record"):
            # recording a test set: kept on the hub (mono, raw mics for good), Eva stays out
            self.recognise(seg, mics, meta, t)
            return
        if not window:
            speech, playback, residual = meta.get("dur", 0), meta.get("playback", 0), meta.get("residual", 1.0)
            if speech < MIN_SPEECH:
                log(f"#{meta.get('seq')} click {speech:.2f}s")
                self.release()
                self.ui("none")
                return
            if playback > ECHO_PLAYBACK and residual is not None and residual < ECHO_RESIDUAL:
                log(f"echo dropped: {speech:.1f}s, residual {residual:.3f}")
                self.release()
                self.ui("none")
                return
            held = None if self.dry else self.merge()
            with self.lock:
                self.last_end = max(self.last_end, meta["end"])
            self.ui("stt")
            if held is not None and (len(held[0]) + len(seg)) / SR > GLUE_MAX:
                # reading aloud without pauses: chained pieces would grow without end
                # (each glue recognised again from the start) - the piece goes alone
                log(f"not joined: {len(held[0]) / SR:.0f} + {len(seg) / SR:.0f} s is over {GLUE_MAX:.0f} s")
                self.utterance(held[2])
                held = None
            if held is not None:
                p0 = float(np.abs(seg).max()) or 1.0
                rest_v = S().spk_emb((seg / p0 * 0.9)[:int(SR * SPK_SECONDS)]).tolist()
                rest_az = None
                if mics is not None:
                    rest_az = (S()._doa_work(mics) or {}).get("az")
                seg, mics, meta = self.glue(held, seg, mics, meta, rest_v, rest_az)
        self.recognise(seg, mics, meta, t)

    def recognise(self, seg, mics, meta, t):
        st = S()
        peak = float(np.abs(seg).max()) or 1.0
        norm = (seg / peak * 0.9).astype(np.float32)
        v = st.spk_emb(norm[:int(SR * SPK_SECONDS)]).round(5).tolist() if len(norm) >= SR // 2 else None
        if mics is not None and not meta.get("window"):
            d = st._doa_work(mics)
            if d:
                meta["doa"] = {k: d[k] for k in ("lags", "peak", "az", "az_strength") if k in d}
        record = bool(meta.get("record"))
        split = not meta.get("window") and not record and meta["dur"] >= SPLIT_MIN
        text, letters = st.decode(norm)
        out = st._place_and_split(norm, text, letters, mics if not meta.get("window") else None, split)
        if not self.dry:
            out = st._finish(norm, out, mics, record, t)
            if record and mics is not None:
                st._keep_raw(mics)
        if record:
            log(f"recorded for the test set: {out.get('text')!r}")
            return
        meta["stt_ms"] = int(1000 * (time.time() - t))
        if out.get("audio"):
            meta["audio"] = out["audio"]
            if meta.get("_joined_from"):
                joined_into(meta["_joined_from"], out["audio"])
        parts = accepted_parts(out.get("parts"))
        if len(parts) > 1:
            log("split " + " | ".join(f"{p['from']}-{p['to']}s"
                                       + (f" [{p['cut']['by']} {p['cut'].get('d', p['cut'].get('whole'))}]" if p.get("cut") else "")
                                       + (f" {p['az']:.0f}°" if p.get("az") is not None else "") + f": {p['text']!r}"
                                       for p in parts))
            for k, p in enumerate(parts):
                m_ = dict(meta, start=meta["start"] + p["from"], speech_start=meta["start"] + p["from"],
                          end=meta["start"] + p["to"], dur=round(p["to"] - p["from"], 2), split=k + 1,
                          audio_span=[p["from"], p["to"] if k < len(parts) - 1 else None])
                if p.get("lags"):
                    m_["doa"] = {"lags": p["lags"], "az": p.get("az"), "peak": (meta.get("doa") or {}).get("peak")}
                a, b = int(p["from"] * SR), int(p["to"] * SR)
                self.deliver(seg[a:b], None, m_, p["text"], p["emb"], hold=False)
            return
        self.deliver(seg, mics, meta, out.get("text") or "", v)

    def deliver(self, seg, mics, meta, text, v, hold=True):
        """A recognised fragment (or a part of one) -> an utterance, held or sent."""
        vb = V()
        raw, text = text, fix(text) if text else text
        if v and vb is not None:
            meta["self_sim"], meta["fam_sim"] = vb.sims(v)
        window = bool(meta.get("window"))
        meta.update(text=text, raw=raw, voice=None if window else v)
        if v and vb is not None and not window and text:
            label, sim, known = vb.identify(v, meta.get("dur", 0), learn=not self.dry)
            meta["speaker"] = {"label": label, "sim": sim, "known": known}
        log(f"#{meta.get('seq')} {'window' if window else 'phrase'} {meta['dur']:.1f}s playback {meta.get('playback', 0):.0%} "
            f"residual {meta.get('residual')} stt {meta.get('stt_ms')}ms self {meta.get('self_sim')} "
            f"fam {meta.get('fam_sim')} {(meta.get('speaker') or {}).get('label', '')}: {text!r}")
        # her own echo is never held: joined with his next phrase it swallowed his
        # "хорошо" (echo "снова я" held, the merge came out "снова я" again, 2026-11-19)
        echo = meta.get("playback", 0) > SHOW_PLAYBACK or (meta.get("self_sim") or 0) >= 0.45
        # a fragment cut by length (the speech went on) waits for its rest like an open end
        if hold and text and not window and not echo and not self.dry and (incomplete(text) or meta.get("chained")):
            self.hold(seg, mics, meta)
        elif text:
            if not window and meta.get("playback", 0) < SHOW_PLAYBACK:
                self.ui("text", text)
            self.utterance(meta)
        elif not window:
            self.ui("none")


def joined_into(piece, whole):
    """A held piece glued with its rest was recognised again as one sound: the
    piece's own clip in the STT log is marked, so the admin page does not show
    the same words twice (2026-11-24 21:46: «…скажу что мы это вчера купили…»
    once as the piece, once as the whole)."""
    import os
    p = os.path.join(S().LOG_DIR, piece.split("#")[0] + ".json")
    try:
        info = json.load(open(p, encoding="utf-8"))
        info["joined_into"] = whole
        with open(p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except (OSError, ValueError) as e:
        log("joined_into:", e)


STATION = Hearing("station")


def config(b):
    """From the brain: the household's name fixes (its profile is private)."""
    global FIXES
    FIXES = [(re.compile(rx), name) for rx, name in b.get("stt_fixes", [])]
    return {"fixes": len(FIXES)}


def parse(body, headers):
    """POST /hear/fragment: X-Meta (json), X-Mono (samples of mono int16 first),
    X-Channels (the raw mics after it, int16 interleaved; 0 = none)."""
    meta = json.loads(headers.get("x-meta", "{}"))
    n = int(headers.get("x-mono", "0"))
    ch = int(headers.get("x-channels", "0"))
    a = np.frombuffer(body[:2 * n], dtype="<i2").astype(np.float32) / 32768.0
    mics = None
    if ch and len(body) > 2 * n:
        mics = np.frombuffer(body[2 * n:], dtype="<i2").reshape(-1, ch)
    return a, mics, meta
