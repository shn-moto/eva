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


OWN_SIM = 0.45                    # her own voice: not kept in the STT log (the brain's SELF_SIM)


# ---- what the sound is (services/voice-events: PANNs) ----
EVENTS_URL = os.environ.get("EVENTS_URL", "http://host.docker.internal:8911/classify")


def sound_of(seg):
    """-> {"voice", "kinds", "verdict", "top"} or None (the service away, too short)."""
    import urllib.request
    if seg is None or len(seg) < SR // 2:
        return None
    pcm = (np.clip(seg, -1, 1) * 32767).astype("<i2").tobytes()
    try:
        req = urllib.request.Request(EVENTS_URL, data=pcm, headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.loads(r.read())
        return None if "error" in out else out
    except Exception as e:
        log("voice-events:", e)
        return None


def emotion_later(seg, audio, text, label, emit=None):
    """The speech emotion (1-2 s) after the phrase went on: into its clip, the log and, by
    `emit`, to the brain (its emotion listeners: a run of anger in a window)."""
    import urllib.request

    def run():
        pcm = (np.clip(seg, -1, 1) * 32767).astype("<i2").tobytes()
        try:
            req = urllib.request.Request(EVENTS_URL.rsplit("/", 1)[0] + "/emotion", data=pcm,
                                         headers={"Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=20) as r:
                emo = json.loads(r.read())
        except Exception as e:
            log("emotion:", e)
            return
        if "error" in emo:
            return
        if audio:
            p = os.path.join(S().LOG_DIR, audio.split("#")[0] + ".json")
            try:
                info = json.load(open(p, encoding="utf-8"))
                info.setdefault("sound", {})["emotion"] = emo
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False)
            except (OSError, ValueError):
                pass
        if emit and audio:
            # the brain ties it to the phrase by the clip (it may come before the phrase did)
            emit({"type": "emotion", "audio": audio.split("#")[0], "label": emo.get("label"),
                  "score": emo.get("score", 0), "probs": emo.get("probs") or {}})
        if emo.get("label") != "neutral" and emo.get("score", 0) >= 0.7:
            log(f"emotion: {emo['label']} {emo['score']} ({label or 'unknown'}): {text!r}")
    threading.Thread(target=run, daemon=True).start()


def mark_sound(audio, sound):
    """The verdict into the clip's json: the admin page shows «шум» / «плач» / «смех»."""
    p = os.path.join(S().LOG_DIR, audio.split("#")[0] + ".json")
    try:
        info = json.load(open(p, encoding="utf-8"))
        info["sound"] = {k: sound[k] for k in ("voice", "verdict", "kinds", "top", "emotion") if k in sound}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except (OSError, ValueError) as e:
        log("mark_sound:", e)


# ---- places: where sound is not listened to (user, 2026-11-26) ----
# The places are marked on the admin page («Отметить место…», the «Места» page; admin-settings
# .json). Each has a mode: "mute" - sound from there is not people in the room (the TV behind
# the Station: a Polish cartoon, Russian-only STT made gibberish of it, guest groups grew from
# its voices, listeners checked it); "only" - listen only to the places marked so, everything
# from other directions is muted. A muted phrase teaches the voices nothing and the brain drops
# it - unless her name is in it and the voice is a family one.
PLACES_FILE = os.environ.get("PLACES_FILE", "/keep/admin-settings.json")
PLACE_TOL = 15.0                 # degrees around the place's mean (one place's phrases spread 2-9)
_PLACES = {"mtime": None, "places": [], "null": [], "null_on": False}


def _places():
    try:
        m = os.path.getmtime(PLACES_FILE)
    except OSError:
        return []
    if m != _PLACES["mtime"]:
        try:
            s = json.load(open(PLACES_FILE, encoding="utf-8"))
        except (OSError, ValueError):
            return _PLACES["places"]
        opts, by = s.get("place_opts") or {}, {}
        for p in s.get("places", []):
            by.setdefault(p["name"], []).append(np.radians(float(p["az"])))
        means = {n: float(np.degrees(np.arctan2(np.sin(a).sum(), np.cos(a).sum()))) % 360 for n, a in by.items()}
        _PLACES.update(mtime=m, places=[(n, a, (opts.get(n) or {}).get("mode")) for n, a in means.items()],
                       # the sound null (ear_null.py) is apart from the logical modes: its own
                       # checkbox per place and a master switch, so the two can be tested apart
                       null=[(n, a) for n, a in means.items() if (opts.get(n) or {}).get("null")],
                       null_on=bool(s.get("null_on")))
        log(f"places: {[(n, round(a), mode) for n, a, mode in _PLACES['places'] if mode]}")
    return _PLACES["places"]


def null_places():
    """-> the places to null by sound [(name, az)], [] when the master switch is off."""
    _places()
    return sorted(_PLACES["null"]) if _PLACES["null_on"] else []


def muted_place(az):
    """-> {"place", "why": "mute" | "outside"} when sound from this angle is not listened to."""
    if az is None:
        return None
    places = _places()
    near = [(n, mode) for n, a, mode in places if abs((float(az) - a + 540) % 360 - 180) <= PLACE_TOL]
    for n, mode in near:
        if mode == "mute":
            return {"place": n, "why": "mute"}
    only = [n for n, a, mode in places if mode == "only"]
    if only and not any(mode == "only" for _, mode in near):
        return {"place": near[0][0] if near else None, "why": "outside"}
    return None


def mark_muted(audio, muted):
    """The clip came from a muted place: the admin page shows it as who."""
    p = os.path.join(S().LOG_DIR, audio.split("#")[0] + ".json")
    try:
        info = json.load(open(p, encoding="utf-8"))
        info["muted"] = muted
        with open(p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except (OSError, ValueError) as e:
        log("mark_muted:", e)


# ---- clean phrases: one voice from one place ----
# The voices learn only from clean phrases (user, 2026-11-26): a phrase where a second
# place speaks (a sustained other direction) is "mixed" - its speaker is told by its head
# (before the other place, at least HEAD_MIN s), and it teaches nothing: no auto-learning,
# no guest group, no enrollment sample, a correction learns from the clean head only.
# A single stray block is a reflection, not a person: 45 % of one-voice phrases have one
# (2026-11-23..26); MIX_RUN blocks in a row MIX_OFF degrees off the head's direction is a
# second place - 13 % of one-voice-labelled phrases (families talking in turn, someone
# walking), and «Неизвестный 215» (a child cutting in at 5.5 s).
MIX_OFF = float(os.environ.get("EVA_HEAR_MIX_OFF", "30"))
MIX_RUN = int(os.environ.get("EVA_HEAR_MIX_RUN", "2"))
HEAD_MIN = 1.5


def _cmed(azs):
    return min(azs, key=lambda c: sum(_apart(a, c) for a in azs))


def mixed_at(blocks):
    """[[t, az, strength], ...] per 0.5 s -> when a second place starts speaking, or None."""
    v = [(b[0], b[1]) for b in blocks or [] if b[1] is not None]
    head = [a for t, a in v if t < SPK_SECONDS] or [a for _, a in v][:6]
    if len(head) < 2:
        return None
    main, run, start = _cmed(head), 0, None
    for t, a in v:
        if _apart(a, main) > MIX_OFF:
            run += 1
            start = t if run == 1 else start
            if run >= MIX_RUN:
                return start
        else:
            run = 0
    return None


def mark_mixed(audio, at, head):
    """The clip's json says so: the admin's correction learns from its head only."""
    p = os.path.join(S().LOG_DIR, audio.split("#")[0] + ".json")
    try:
        info = json.load(open(p, encoding="utf-8"))
        info.update(mixed=at, head=head)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except (OSError, ValueError) as e:
        log("mark_mixed:", e)


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


# A change of place is believed only when the two parts' angles differ this much.
# The direction distance alone (DIR_CUT 0.05) cut one person standing still:
# of 124 direction cuts with both angles (2026-11-24/25) 75 were < 20 degrees
# apart, 54 < 10 - one place (the test set's places spread 2-9 degrees; places
# in the room are 30+ apart); the distance itself overlapped for true and false
# cuts (0.05-0.26 vs 0.05-0.59). A book read aloud from one spot was cut twice
# at 6 and 0 degrees (2026-11-25).
DIR_AZ = float(os.environ.get("EVA_HEAR_DIR_AZ", "20"))


def _apart(a, b):
    return abs((a - b + 180) % 360 - 180)


def place_changed(prev, part):
    a, b = prev.get("az"), part.get("az")
    return a is not None and b is not None and _apart(a, b) >= DIR_AZ


def accepted_parts(parts):
    """The parts with the cuts believed: a change of place when the angles
    really differ (DIR_AZ), a change of voice by cut_believed. A cut not
    believed joins its two parts back."""
    if not parts:
        return []
    out = [dict(parts[0])]
    for p in parts[1:]:
        cut = p.get("cut") or {}
        ok = (cut.get("by") == "direction" and place_changed(out[-1], p)) or (
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
        # what the sound is, in parallel with STT (~0.2 s on the CPU, no added wait)
        snd = {}
        th = None
        if not meta.get("window") and not record:
            th = threading.Thread(target=lambda: snd.update(r=sound_of(seg)), daemon=True)
            th.start()
        text, letters = st.decode(norm)
        if meta.get("music") and mics is not None and not meta.get("window") and not record:
            # over her music AEC3 + NS gate the room to -66 dB between words and bite their starts:
            # «Ева, стоп» came out «евосток» and the music could not be stopped, while the raw mics,
            # music and all, read «ева стоп» (2026-11-27 12:34). The raw mix goes to STT as well;
            # the brain takes its words when only they carry her name or a stop (dialog.py).
            t_raw = time.time()
            raw = mics.astype(np.float32).mean(axis=1)
            rp = float(np.abs(raw).max()) or 1.0
            meta["raw_text"] = st.decode((raw / rp * 0.9).astype(np.float32))[0]
            log(f"#{meta.get('seq')} over music: {text!r} / raw mics {meta['raw_text']!r} "
                f"({int(1000 * (time.time() - t_raw))} ms)")
        out = st._place_and_split(norm, text, letters, mics if not meta.get("window") else None, split)
        mix = mixed_at((out.get("doa") or {}).get("blocks"))
        if mix is not None:
            # two places in one phrase: the voice is told by the head, before the other place
            meta["mixed"], meta["head"] = mix, round(min(SPK_SECONDS, mix), 2) if mix >= HEAD_MIN else None
            if meta["head"] and meta["head"] < SPK_SECONDS:
                v = st.spk_emb(norm[:int(SR * meta["head"])]).round(5).tolist()
        if meta.get("window"):
            # heard over her own voice (a window to catch «стоп»): kept as such - most
            # of it is her echo, and it was taken for a family voice (2026-11-25 10:09)
            out["window"], out["playback"] = True, meta.get("playback")
        # her own voice only (a window over her speech, or the tail that ends after it): it goes to
        # the brain (a «стоп» is looked for there) but not into the log - one reply of hers was
        # three entries with a word twice: «включаю» / «я хочу быть» / «хочу быть с тобой»,
        # the windows overlap 0.6 s (2026-11-26). Another voice in it: logged as before.
        own = False
        vb = V()
        if v and vb is not None and (meta.get("window") or meta.get("playback", 0) >= 0.5):
            self_sim, fam_sim = vb.sims(v)
            own = self_sim >= OWN_SIM and self_sim > fam_sim + 0.1
        if not self.dry and not own:
            out = st._finish(norm, out, mics, record, t)
            if record and mics is not None:
                st._keep_raw(mics)
        if record:
            log(f"recorded for the test set: {out.get('text')!r}")
            return
        meta["stt_ms"] = int(1000 * (time.time() - t))
        if th is not None:
            th.join(timeout=3)
            if snd.get("r"):
                meta["sound"] = snd["r"]
                s_ = snd["r"]
                if s_["verdict"] != "speech" or max(s_["kinds"].get(k, 0) for k in ("cry", "scream", "shout", "laugh")) >= 0.3:
                    # stage 2 of the emotion plan: every event logged, no action yet
                    log(f"sound: {s_['verdict']} voice {s_['voice']} {s_['kinds']} top {s_['top'][:2]}: {text!r}")
        if out.get("audio"):
            meta["audio"] = out["audio"]
            if meta.get("raw_text") is not None:
                note_field(out["audio"], "raw_text", meta["raw_text"])      # for the cut's redo: both readings kept
            if meta.get("sound"):
                mark_sound(out["audio"], meta["sound"])
        if (meta.get("sound") or {}).get("emotion_wanted") and not self.dry:
            emotion_later(seg, meta.get("audio"), out.get("text") or "", None, emit=self.emit)
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
        muted = None if window else muted_place((meta.get("doa") or {}).get("az"))
        if muted:
            meta["muted"] = muted
            if not self.dry and meta.get("audio") and text:
                mark_muted(meta["audio"], muted)
        if v and vb is not None and not window and text:
            # a mixed phrase teaches nothing (mixed_at), nor one from a muted place: it is only recognised
            label, sim, known = vb.identify(v, meta.get("dur", 0),
                                            learn=not self.dry and meta.get("mixed") is None and not muted)
            meta["speaker"] = {"label": label, "sim": sim, "known": known}
            if not known:
                # not signed (too short, too close): the nearest family voice goes along as a guess
                n, s, lead = vb.nearest(v)
                if n:
                    meta["speaker"]["nearest"] = {"label": n, "sim": round(s, 3), "lead": round(lead, 3)}
            if not self.dry and meta.get("audio"):
                note_who(meta["audio"], meta.get("audio_span"), label, sim)
                # the phrase's print, whole, kept once: a rescan reads it instead of the sound
                try:
                    full = v
                    if meta.get("mixed") is not None:
                        mark_mixed(meta["audio"], meta["mixed"], meta.get("head"))
                    elif seg is not None and len(seg) > SR * (SPK_SECONDS + 0.5):
                        p0 = float(np.abs(seg).max()) or 1.0
                        full = S().spk_emb((seg / p0 * 0.9).astype(np.float32)).round(5).tolist()
                    vb.store_print(memory_key(meta["audio"], meta.get("audio_span")), full, meta.get("dur", 0))
                except Exception as e:
                    log("print not kept:", e)
        log(f"#{meta.get('seq')} {'window' if window else 'phrase'} {meta['dur']:.1f}s playback {meta.get('playback', 0):.0%} "
            f"residual {meta.get('residual')} stt {meta.get('stt_ms')}ms self {meta.get('self_sim')} "
            f"fam {meta.get('fam_sim')} {(meta.get('speaker') or {}).get('label', '')}"
            f"{' [muted: ' + str(muted['place'] or 'outside') + ']' if muted else ''}: {text!r}")
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


def memory_key(audio, span):
    """The clip as the memory names it (app/memory.py write): <day>/<time>[#t=from[,to]]."""
    if span and span[1]:
        return audio + f"#t={span[0]:g},{span[1]:g}"
    if span and span[0]:
        return audio + f"#t={span[0]:g}"
    return audio


def note_field(audio, key, value):
    """One more fact into the clip's json in the STT log."""
    import os
    p = os.path.join(S().LOG_DIR, audio.split("#")[0] + ".json")
    try:
        info = json.load(open(p, encoding="utf-8"))
        info[key] = value
        with open(p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except (OSError, ValueError) as e:
        log("note_field:", e)


def note_who(audio, span, label, sim):
    """Who said it, kept with the clip in the STT log ("who": per part): the
    admin page shows the voices she did not know by their group - the same
    stranger's phrases under one «Неизвестный N» (2026-11-25)."""
    import os
    p = os.path.join(S().LOG_DIR, audio.split("#")[0] + ".json")
    try:
        info = json.load(open(p, encoding="utf-8"))
        who = [w for w in info.get("who", []) if w.get("from") != (span or [0])[0]]
        who.append({"from": (span or [0, None])[0], "to": (span or [0, None])[1], "label": label, "sim": sim})
        info["who"] = sorted(who, key=lambda w: w.get("from") or 0)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except (OSError, ValueError) as e:
        log("note_who:", e)


def mark_echo(audio, flag="echo"):
    """The brain took this clip for her own voice: marked at once, as the rescan
    marks it (rescan.mark_echo) - the admin page shows «Ева (эхо)» right away."""
    import os
    if not audio or "/" not in audio or ".." in audio:
        return False
    p = os.path.join(S().LOG_DIR, audio.split("#")[0] + ".json")
    try:
        info = json.load(open(p, encoding="utf-8"))
        if flag not in ("echo", "noise"):
            return False
        if info.get(flag):
            return True
        info[flag] = True
        with open(p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        return True
    except (OSError, ValueError) as e:
        log("mark_echo:", e)
        return False


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
