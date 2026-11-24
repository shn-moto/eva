"""Who is speaking: CAM++ voice embeddings (sherpa-onnx) matched against the
family's voice profiles; unknown voices gather in stable "гость-N" clusters, so
when a voice is named later ("Ева, запомни мой голос, я Маша") its past phrases
get that name too.

Ported from the old assistant.py (2026-11-04): same model, same files, same
thresholds. The embedding is computed in ear.py (it has the audio), matching
and the profiles live here, in the brain.
"""
import json
import os
import threading
import time

import numpy as np

from core import profile

from core.util import log

DIR = "/var/lib/eva/voices"
PROFILES = os.path.join(DIR, "speakers.json")
CLUSTERS = os.path.join(DIR, "speaker_clusters.json")
AUTO_FILE = os.path.join(DIR, "speakers_auto.json")
AUTO_MAX, AUTO_MIN_SEC, AUTO_MIN_SIM, AUTO_MIN_LEAD = 30, 2.0, 0.6, 0.2
THRESHOLD = profile.tune("voices.threshold", 0.5)
ID_MARGIN = profile.tune("voices.margin", 0.1)             # over the next known voice (profiles cross at up to 0.49)
LOW_THRESHOLD, LOW_MARGIN = profile.tune("voices.low_threshold", 0.42), profile.tune("voices.low_margin", 0.15)   # a weaker match (another room) with a clear lead
SHORT_SEC, SHORT_MIN = profile.tune("voices.short_sec", 1.5), profile.tune("voices.short_min", 0.7)     # a phrase shorter than this needs a strong match to be named
CLAIM_MIN = profile.tune("voices.claim_min", 0.38)           # «запомни мой голос, я Иван» from a voice this close to Иван: a refresh
ENROLL_MATCH = profile.tune("voices.enroll_match", 0.55)         # an enrollment sample / a guest cluster like the introduction phrase
# an enrollment sample must be closer to the introduction phrase than to anybody
# else's profile by this much. Not an absolute "like another voice" cap (was 0.5):
# sisters are 0.62-0.67 to each other, so Аврора's samples were all thrown away
# as "Аэлита's" and she could not be enrolled (2026-11-24)
ENROLL_LEAD = profile.tune("voices.enroll_lead", 0.05)
# a known voice saying it is a family member with no voice yet is enrolled under
# that name unless it is this sure to be the voice it was taken for
ENROLL_KNOWN_MAX = profile.tune("voices.enroll_known_max", 0.75)
CLUSTER_MATCH = profile.tune("voices.cluster_match", 0.62)
CLUSTER_MAX_EMB = 10
NEXT_GUEST = [0]            # the next guest number in this run (pruned numbers are not given again)
CLUSTER_MIN_SEC = profile.tune("voices.cluster_min_sec", 1.2)       # short shouts are not clustered
# profile names as stored -> how Eva calls them (the old profile is "Alex")
CALL = profile.VOICE_CALL
# voice profile -> person in the photolib family tree (one Иван for voice and Telegram)
PERSON = profile.VOICE_PERSON
# every name a profile answers to: "я Иван" from Alex is not an impostor
ALIASES = profile.VOICE_ALIASES


def same_person(profile, name):
    """Does `name` (as said in an introduction) mean this profile?"""
    n = name.lower()
    return n == profile.lower() or n in ALIASES.get(profile, set()) or n == CALL.get(profile, "").lower()


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Voices:
    def __init__(self):
        self.lock = threading.Lock()
        self.profiles = _load(PROFILES)      # name -> [embedding, ...]
        self.clusters = _load(CLUSTERS)
        self.auto = _load(AUTO_FILE)         # name -> [embedding]: learnt from recognised phrases      # "гость-N" -> {"embs", "count", "first", "last"}
        self.enroll = {"name": None, "embs": [], "until": 0.0}
        self.pruned = 0.0
        self.prune()

    def prune(self):
        """Guest clusters that are noise: one phrase heard more than a day ago,
        a rare voice (< 5 phrases) not heard for a month, and a family voice
        that was not recognised then (Лиза before her enrollment, Иван in
        another room) - the profiles know it now. 78 had piled up, 62 of them
        single phrases (2026-11-20)."""
        now, drop = time.time(), []
        for g, c in self.clusters.items():
            age = now - c.get("last", 0)
            if (c.get("count", 1) <= 1 and age > 86400) or (c.get("count", 1) < 5 and age > 30 * 86400):
                drop.append((g, "old"))
                continue
            sims = sorted(((float(np.mean([max(float(np.dot(np.asarray(e, dtype=np.float32), np.asarray(p, dtype=np.float32)))
                                                 for p in ps) for e in c["embs"]])), k)
                           for k, ps in self.profiles.items() if ps), reverse=True)
            if sims and sims[0][0] >= LOW_THRESHOLD and (len(sims) < 2 or sims[0][0] - sims[1][0] >= LOW_MARGIN):
                drop.append((g, f"family voice {sims[0][1]} {sims[0][0]:.2f}"))
        for g, why in drop:
            del self.clusters[g]
        if drop:
            _save(CLUSTERS, self.clusters)
            log(f"guest clusters pruned: {len(drop)} ({sum(w == 'old' for _, w in drop)} old), left {len(self.clusters)}")
        self.pruned = now

    def names(self):
        return sorted(self.profiles)

    @staticmethod
    def _best(emb, groups):
        best, sim = None, -1.0
        for name, embs in groups:
            for p in embs:
                s = float(np.dot(emb, np.asarray(p, dtype=np.float32)))
                if s > sim:
                    best, sim = name, s
        return best, sim

    def claim(self, emb, name, lead=ID_MARGIN):
        """Is a voice saying «я <name>» likely that person? Its own profile must
        lead the others by `lead`: clearly (ID_MARGIN) for an unrecognised voice
        in other acoustics (another room); a negative lead - "not clearly somebody
        else" - for close voices like sisters, which pass for each other."""
        e = np.asarray(emb, dtype=np.float32)
        e = e / (np.linalg.norm(e) + 1e-9)
        with self.lock:
            own = [p for p in self.profiles if same_person(p, name)]
            if not own:
                return None, 0.0
            mine = self._best(e, ((k, v) for k, v in self.profiles.items() if k in own))
            other = self._best(e, ((k, v) for k, v in self.profiles.items() if k not in own))[1]
        ok = mine[1] >= CLAIM_MIN and mine[1] - other >= lead
        return (mine[0] if ok else None), round(mine[1], 3)

    def _voices(self):
        """The enrolled samples plus the ones learnt from life (AUTO_FILE)."""
        return {k: list(v) + self.auto.get(k, []) for k, v in self.profiles.items()}

    def _learn(self, name, emb, sim, second, dur):
        """A long phrase recognised beyond doubt adds its print (kept apart, the
        newest AUTO_MAX per person): recognition follows the rooms and the
        distances she hears a voice from - in another room Иван's 1.7 s phrase
        was 0.39 and a stranger (2026-11-20). Strict, so no other voice gets in."""
        if dur < AUTO_MIN_SEC or sim < AUTO_MIN_SIM or sim - second < AUTO_MIN_LEAD or sim > 0.97:
            return
        lst = self.auto.setdefault(name, [])
        lst.append(emb.tolist())
        del lst[:-AUTO_MAX]
        _save(AUTO_FILE, self.auto)

    def identify(self, emb, dur):
        """(label, similarity, known). label: profile name, or a guest cluster."""
        emb = np.asarray(emb, dtype=np.float32)
        emb = emb / (np.linalg.norm(emb) + 1e-9)
        with self.lock:
            voices = self._voices()
            name, sim = self._best(emb, voices.items())
            learning = self.enroll["name"] if time.time() < self.enroll["until"] else None
            if learning:
                # a known voice of somebody else cutting in is not a sample of this
                # one: it is closer to that voice than to the introduction phrase
                first = self.enroll["embs"][0] if self.enroll["embs"] else None
                own = float(emb @ np.asarray(first, dtype=np.float32)) if first is not None else 0.0
                if not (name is not None and sim >= THRESHOLD and not same_person(name, learning)
                        and sim - own >= ENROLL_LEAD):
                    self.enroll["embs"].append(emb.tolist())
            if name is not None and dur < SHORT_SEC and not (sim >= SHORT_MIN and sim - self._best(
                    emb, ((k, v) for k, v in voices.items() if k != name))[1] >= LOW_MARGIN):
                # one short word says little about who: «хорошо» (1.2 s) was taken
                # for Маша at 0.62 when she had left the house (2026-11-20)
                return "unknown", round(sim, 3), False
            if name is not None and sim >= LOW_THRESHOLD:
                # a known name only with a lead over the other known voices: in a
                # crowded room a mix of voices scored 0.51-0.55 for Иван and Маша
                # alike and she called Иван "Маша" (2026-11-19). Below THRESHOLD
                # a bigger lead is enough: in another room Иван scored 0.44-0.49
                # (others 0.25) and was a stranger for her (2026-11-19 evening)
                _, second = self._best(emb, ((k, v) for k, v in voices.items() if k != name))
                if sim - second >= (ID_MARGIN if sim >= THRESHOLD else LOW_MARGIN):
                    self._learn(name, emb, sim, second, dur)
                    return name, round(sim, 3), True
                if sim >= THRESHOLD:
                    log(f"voice: {name} {sim:.2f} vs next {second:.2f} - too close, not recognised")
                    return "unknown", round(sim, 3), False
            if dur < CLUSTER_MIN_SEC:
                return "unknown", round(sim, 3), False
            if time.time() - self.pruned > 6 * 3600:
                self.prune()
            g, gsim = self._best(emb, ((k, v["embs"]) for k, v in self.clusters.items()))
            if g is not None and gsim >= CLUSTER_MATCH:
                cl = self.clusters[g]
                cl["count"] = cl.get("count", 0) + 1
                cl["last"] = time.time()
                if len(cl["embs"]) < CLUSTER_MAX_EMB and gsim < 0.85:
                    cl["embs"].append(emb.tolist())
                _save(CLUSTERS, self.clusters)
                return g, round(gsim, 3), False
            # numbers are not reused: two different voices were both "гость-72" in one log
            n = max((int(k.split("-")[1]) for k in self.clusters if k.split("-")[-1].isdigit()), default=0) + 1
            n = max(n, NEXT_GUEST[0])
            NEXT_GUEST[0] = n + 1
            g = f"гость-{n}"
            self.clusters[g] = {"embs": [emb.tolist()], "count": 1, "first": time.time(), "last": time.time()}
            _save(CLUSTERS, self.clusters)
            return g, 0.0, False

    # --- "Ева, запомни мой голос, я Маша" ---
    def start_enroll(self, name, seconds=60, first=None):
        """first: the embedding of the introduction phrase itself, the first sample."""
        with self.lock:
            embs = []
            if first:
                e = np.asarray(first, dtype=np.float32)
                embs.append((e / (np.linalg.norm(e) + 1e-9)).tolist())
            self.enroll = {"name": name, "embs": embs, "until": time.time() + seconds}

    def enrolling(self):
        return self.enroll["name"] if time.time() < self.enroll["until"] else None

    def _near_other(self, emb, name):
        """The best similarity of a sample to the profiles of other people."""
        e = np.asarray(emb, dtype=np.float32)
        return max((float(e @ np.asarray(p, dtype=np.float32)) for k, v in self.profiles.items() if k != name
                    for p in v), default=0.0)

    def finish_enroll(self):
        """Profile += collected samples; guest clusters of the same voice merge
        into it. -> (name, samples, merged clusters)

        Strict, because others talk during an enrollment: Лиза's profile got
        6 samples of Маша's voice (0.51-0.66 to Маша) from merged guest
        clusters (2026-11-19). A sample must be like the introduction phrase
        and not like anybody else; a cluster merges only as a whole voice
        (its average like the samples), and only its clean samples."""
        with self.lock:
            name, embs = self.enroll["name"], self.enroll["embs"]
            self.enroll = {"name": None, "embs": [], "until": 0.0}
            if not name or not embs:
                return name, 0, []
            first = np.asarray(embs[0], dtype=np.float32)
            def own(e):
                return float(first @ np.asarray(e, dtype=np.float32))
            embs = [embs[0]] + [e for e in embs[1:] if own(e) >= ENROLL_MATCH
                                and own(e) - self._near_other(e, name) >= ENROLL_LEAD]
            self.profiles.setdefault(name, []).extend(embs)
            mine = [np.asarray(e, dtype=np.float32) for e in embs]
            moved = []
            for g in list(self.clusters):
                ce = self.clusters[g]["embs"]
                avg = np.mean([max(float(m @ np.asarray(c, dtype=np.float32)) for m in mine) for c in ce])
                if avg < ENROLL_MATCH:
                    continue
                self.profiles[name].extend(
                    c for c in ce if max(float(m @ np.asarray(c, dtype=np.float32)) for m in mine)
                    - self._near_other(c, name) >= ENROLL_LEAD)
                moved.append(g)
                del self.clusters[g]
            _save(PROFILES, self.profiles)
            if moved:
                _save(CLUSTERS, self.clusters)
            return name, len(embs), moved
