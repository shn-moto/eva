"""The voice base, on the hub: who is speaking. CAM++ prints of the family's
voices in Postgres (assistant_memory), matched here; unknown voices gather in
stable "гость-N" clusters; voices learnt from life, from «запомни мой голос»
and from the speakers corrected on the admin page - all in one place.

Moved from the Station's brain (app/voices.py, files in /var/lib/eva/voices)
2026-11-24 (docs/EAR-PLAN.md, stage 1): the Station only hears, every decision
about the sound is made on the hub. Same thresholds, same rules.

Tables (created by the owner once, see SCHEMA):
  voice_prints (speaker, emb, source enroll|correction|auto|cluster|self, ...)
  voice_guests (name, embs, count, first, last)
  voice_people (name, aliases, family): the household's names, sent by the
      brain at its start (the household profile is private, on the Station)
"""
import json
import os
import threading
import time

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_prints (
    id bigserial PRIMARY KEY, speaker text NOT NULL, emb real[] NOT NULL,
    source text NOT NULL, audio text, utterance_id bigint, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS voice_prints_speaker ON voice_prints (speaker);
CREATE TABLE IF NOT EXISTS voice_guests (
    name text PRIMARY KEY, embs jsonb NOT NULL, count int NOT NULL DEFAULT 1,
    first timestamptz NOT NULL DEFAULT now(), last timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS voice_people (
    name text PRIMARY KEY, aliases text[] NOT NULL DEFAULT '{}', family boolean NOT NULL DEFAULT true);
GRANT SELECT, INSERT, UPDATE, DELETE ON voice_prints, voice_guests, voice_people TO assistant;
GRANT USAGE ON SEQUENCE voice_prints_id_seq TO assistant;
"""


def tune(key, default):
    """EVA_<KEY> from the environment, else the value measured here."""
    v = os.environ.get("EVA_" + key.upper().replace(".", "_"))
    return default if v is None else type(default)(v)


AUTO_MAX, AUTO_MIN_SEC, AUTO_MIN_SIM, AUTO_MIN_LEAD = 30, 2.0, 0.6, 0.2
THRESHOLD = tune("voices.threshold", 0.5)
ID_MARGIN = tune("voices.margin", 0.1)             # over the next known voice (profiles cross at up to 0.49)
LOW_THRESHOLD, LOW_MARGIN = tune("voices.low_threshold", 0.42), tune("voices.low_margin", 0.15)   # a weaker match (another room) with a clear lead
SHORT_SEC, SHORT_MIN = tune("voices.short_sec", 1.5), tune("voices.short_min", 0.7)     # a phrase shorter than this needs a strong match to be named
CLAIM_MIN = tune("voices.claim_min", 0.38)         # «запомни мой голос, я Иван» from a voice this close to Иван: a refresh
ENROLL_MATCH = tune("voices.enroll_match", 0.55)   # an enrollment sample / a guest cluster like the introduction phrase
# an enrollment sample must be closer to the introduction phrase than to anybody
# else's profile by this much. Not an absolute "like another voice" cap (was 0.5):
# sisters are 0.62-0.67 to each other, so the younger one's samples were all
# thrown away as the elder's and she could not be enrolled (2026-11-24)
ENROLL_LEAD = tune("voices.enroll_lead", 0.05)
CLUSTER_MATCH = tune("voices.cluster_match", 0.62)
CLUSTER_MAX_EMB = 10
CLUSTER_MIN_SEC = tune("voices.cluster_min_sec", 1.2)       # short shouts are not clustered
ENROLL_SECONDS = 60
SELF = "Ева"                    # her own voice (source "self"): echo, not a person


def _unit(e):
    e = np.asarray(e, dtype=np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


def log(*a):
    print(time.strftime("%H:%M:%S"), "voices:", *a, flush=True)


class VoiceBase:
    def __init__(self, dsn):
        self.dsn = dsn
        self.lock = threading.Lock()
        self.conn = None
        self.profiles = {}            # name -> [emb]: enroll + correction + cluster prints
        self.auto = {}                # name -> [(id, emb)]: learnt from recognised phrases, newest last
        self.clusters = {}            # "гость-N" -> {"embs", "count", "first", "last"}
        self.self_ = []               # her own voice
        self.people = {}              # name -> {"aliases": set, "family": bool}
        self.enroll = {"name": None, "embs": [], "until": 0.0}
        self.next_guest = 0
        self.pruned = 0.0
        self.loaded = False

    # --- the base ---
    def db(self):
        import psycopg
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
        return self.conn

    def _exec(self, sql, args=()):
        """A write; the base away -> logged, the voice stays in memory till a restart."""
        try:
            return self.db().execute(sql, args)
        except Exception as e:
            log("db write:", e)
            self.conn = None
            return None

    def load(self):
        with self.lock:
            c = self.db()
            self.profiles, self.auto, self.self_ = {}, {}, []
            for pid, spk, emb, src in c.execute("SELECT id, speaker, emb, source FROM voice_prints ORDER BY id"):
                e = _unit(emb).tolist()
                if src == "self":
                    self.self_.append(e)
                elif src == "auto":
                    self.auto.setdefault(spk, []).append((pid, e))
                else:
                    self.profiles.setdefault(spk, []).append(e)
            self.clusters = {n: {"embs": embs, "count": cnt, "first": f.timestamp(), "last": l.timestamp()}
                             for n, embs, cnt, f, l in c.execute("SELECT name, embs, count, first, last FROM voice_guests")}
            self.people = {n: {"aliases": {a.lower() for a in al}, "family": fam}
                           for n, al, fam in c.execute("SELECT name, aliases, family FROM voice_people")}
            self.loaded = True
        log(f"loaded: {', '.join(f'{k} {len(v)}+{len(self.auto.get(k, []))}' for k, v in self.profiles.items())}; "
            f"guests {len(self.clusters)}, self {len(self.self_)}")

    def _add_print(self, speaker, emb, source, audio=None, utterance_id=None):
        r = self._exec("INSERT INTO voice_prints (speaker, emb, source, audio, utterance_id) VALUES (%s,%s,%s,%s,%s)"
                       " RETURNING id", (speaker, [float(x) for x in emb], source, audio, utterance_id))
        return r.fetchone()[0] if r else None

    def _save_guest(self, g):
        c = self.clusters[g]
        self._exec("INSERT INTO voice_guests (name, embs, count, first, last) VALUES (%s,%s,%s,to_timestamp(%s),to_timestamp(%s))"
                   " ON CONFLICT (name) DO UPDATE SET embs = EXCLUDED.embs, count = EXCLUDED.count, last = EXCLUDED.last",
                   (g, json.dumps(c["embs"]), c.get("count", 1), c.get("first", time.time()), c.get("last", time.time())))

    def _drop_guests(self, names):
        if names:
            self._exec("DELETE FROM voice_guests WHERE name = ANY(%s)", (list(names),))

    # --- the household's names ---
    def set_people(self, people):
        """[{name, aliases, family}] from the brain (its private profile)."""
        with self.lock:
            for p in people:
                self.people[p["name"]] = {"aliases": {a.lower() for a in p.get("aliases", [])},
                                          "family": bool(p.get("family", True))}
                self._exec("INSERT INTO voice_people (name, aliases, family) VALUES (%s,%s,%s) ON CONFLICT (name)"
                           " DO UPDATE SET aliases = EXCLUDED.aliases, family = EXCLUDED.family",
                           (p["name"], sorted(self.people[p["name"]]["aliases"]), self.people[p["name"]]["family"]))

    def same_person(self, prof, name):
        n = name.lower()
        return n == prof.lower() or n in self.people.get(prof, {}).get("aliases", set())

    def _profile_for(self, name):
        """The voice profile a name means: an existing one, or a new one for a
        family member with no voice yet; None for guests and the like."""
        own = next((p for p in self.profiles if self.same_person(p, name)), None)
        if own:
            return own
        fam = next((p for p, v in self.people.items() if v["family"] and self.same_person(p, name)), None)
        return fam

    # --- matching ---
    def _voices(self):
        """Every print of a person: the honest ones and the learnt ones."""
        return {k: list(v) + [e for _, e in self.auto.get(k, [])] for k, v in self.profiles.items()}

    @staticmethod
    def _best(emb, groups):
        best, sim = None, -1.0
        for name, embs in groups:
            for p in embs:
                s = float(np.dot(emb, np.asarray(p, dtype=np.float32)))
                if s > sim:
                    best, sim = name, s
        return best, sim

    def _near_other(self, emb, name):
        e = np.asarray(emb, dtype=np.float32)
        return max((float(e @ np.asarray(p, dtype=np.float32)) for k, v in self.profiles.items() if k != name
                    for p in v), default=0.0)

    def self_sim(self, emb):
        e = _unit(emb)
        return max((float(e @ np.asarray(p, dtype=np.float32)) for p in self.self_), default=0.0)

    def prune(self):
        """Guest clusters that are noise: one phrase heard more than a day ago,
        a rare voice (< 5 phrases) not heard for a month, and a family voice
        that was not recognised then - the profiles know it now."""
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
        for g, _ in drop:
            del self.clusters[g]
        self._drop_guests(g for g, _ in drop)
        if drop:
            log(f"guest clusters pruned: {len(drop)} ({sum(w == 'old' for _, w in drop)} old), left {len(self.clusters)}")
        self.pruned = now

    def _learn(self, name, emb, sim, second, dur):
        """A long phrase recognised beyond doubt adds its print (the newest
        AUTO_MAX per person): recognition follows the rooms and distances."""
        if dur < AUTO_MIN_SEC or sim < AUTO_MIN_SIM or sim - second < AUTO_MIN_LEAD or sim > 0.97:
            return
        pid = self._add_print(name, emb.tolist(), "auto")
        lst = self.auto.setdefault(name, [])
        lst.append((pid, emb.tolist()))
        old = lst[:-AUTO_MAX]
        del lst[:-AUTO_MAX]
        if old:
            self._exec("DELETE FROM voice_prints WHERE id = ANY(%s)", ([i for i, _ in old if i],))

    def identify(self, emb, dur):
        """(label, similarity, known). label: profile name, or a guest cluster."""
        emb = _unit(emb)
        with self.lock:
            voices = self._voices()
            name, sim = self._best(emb, voices.items())
            learning = self.enroll["name"] if time.time() < self.enroll["until"] else None
            if learning:
                # a known voice of somebody else cutting in is not a sample of this
                # one: it is closer to that voice than to the introduction phrase
                first = self.enroll["embs"][0] if self.enroll["embs"] else None
                own = float(emb @ np.asarray(first, dtype=np.float32)) if first is not None else 0.0
                if not (name is not None and sim >= THRESHOLD and not self.same_person(name, learning)
                        and sim - own >= ENROLL_LEAD):
                    self.enroll["embs"].append(emb.tolist())
            if name is not None and dur < SHORT_SEC and not (sim >= SHORT_MIN and sim - self._best(
                    emb, ((k, v) for k, v in voices.items() if k != name))[1] >= LOW_MARGIN):
                # one short word says little about who: «хорошо» (1.2 s) was taken
                # for somebody who had left the house (2026-11-20)
                return "unknown", round(sim, 3), False
            if name is not None and sim >= LOW_THRESHOLD:
                # a known name only with a lead over the other known voices; below
                # THRESHOLD a bigger lead is enough (another room)
                _, second = self._best(emb, ((k, v) for k, v in voices.items() if k != name))
                if sim - second >= (ID_MARGIN if sim >= THRESHOLD else LOW_MARGIN):
                    self._learn(name, emb, sim, second, dur)
                    return name, round(sim, 3), True
                if sim >= THRESHOLD:
                    log(f"{name} {sim:.2f} vs next {second:.2f} - too close, not recognised")
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
                self._save_guest(g)
                return g, round(gsim, 3), False
            # numbers are not reused: two different voices were both "гость-72" in one log
            n = max((int(k.split("-")[1]) for k in self.clusters if k.split("-")[-1].isdigit()), default=0) + 1
            n = max(n, self.next_guest)
            self.next_guest = n + 1
            g = f"гость-{n}"
            self.clusters[g] = {"embs": [emb.tolist()], "count": 1, "first": time.time(), "last": time.time()}
            self._save_guest(g)
            return g, 0.0, False

    def claim(self, emb, name, lead=ID_MARGIN):
        """Is a voice saying «я <name>» likely that person? Its own profile must
        lead the others by `lead`: clearly for an unrecognised voice in other
        acoustics; a negative lead - "not clearly somebody else" - for close
        voices like sisters, which pass for each other."""
        e = _unit(emb)
        with self.lock:
            own = [p for p in self.profiles if self.same_person(p, name)]
            if not own:
                return None, 0.0
            mine = self._best(e, ((k, v) for k, v in self.profiles.items() if k in own))
            other = self._best(e, ((k, v) for k, v in self.profiles.items() if k not in own))[1]
        ok = mine[1] >= CLAIM_MIN and mine[1] - other >= lead
        return (mine[0] if ok else None), round(mine[1], 3)

    # --- «Ева, запомни мой голос, я Маша» ---
    def start_enroll(self, name, first=None, seconds=ENROLL_SECONDS):
        with self.lock:
            self.enroll = {"name": name, "embs": [_unit(first).tolist()] if first else [],
                           "until": time.time() + seconds}

    def enrolling(self):
        with self.lock:
            on = time.time() < self.enroll["until"]
            return {"name": self.enroll["name"] if on else None, "samples": len(self.enroll["embs"]) if on else 0}

    def finish_enroll(self):
        """Profile += the collected samples; guest clusters of the same voice
        merge into it. -> (name, samples, merged clusters)"""
        with self.lock:
            name, embs = self.enroll["name"], self.enroll["embs"]
            self.enroll = {"name": None, "embs": [], "until": 0.0}
            if not name or not embs:
                return name, 0, []
            return self._enroll(name, embs, source="enroll")

    def _enroll(self, name, embs, source, trusted=False, meta=None):
        """name's voice += samples (all of them when trusted, else those like the
        first, the introduction phrase); guest clusters of that voice merge in.
        Strict, because others talk during an enrollment: a cluster merges only
        as a whole voice (its average like the samples), and only its clean
        samples. Called with the lock held."""
        if not trusted:
            first = np.asarray(embs[0], dtype=np.float32)

            def own(e):
                return float(first @ np.asarray(e, dtype=np.float32))
            embs = [embs[0]] + [e for e in embs[1:] if own(e) >= ENROLL_MATCH
                                and own(e) - self._near_other(e, name) >= ENROLL_LEAD]
        have = self.profiles.setdefault(name, [])
        new = []
        for k, e in enumerate(embs):
            if not any(float(np.dot(e, p)) > 0.99 for p in have + new):
                new.append(e)
                m = (meta or [{}] * len(embs))[k] if meta else {}
                self._add_print(name, e, source, m.get("audio"), m.get("utterance_id"))
        have.extend(new)
        mine = [np.asarray(e, dtype=np.float32) for e in embs]
        moved = []
        for g in list(self.clusters):
            ce = self.clusters[g]["embs"]
            avg = np.mean([max(float(m @ np.asarray(c, dtype=np.float32)) for m in mine) for c in ce])
            if avg < ENROLL_MATCH:
                continue
            take = [c for c in ce if max(float(m @ np.asarray(c, dtype=np.float32)) for m in mine)
                    - self._near_other(c, name) >= ENROLL_LEAD]
            if trusted and not take:
                continue            # a voice near hers, not hers: the cluster stays a guest
            for c in take:
                self._add_print(name, c, "cluster")
            have.extend(take)
            moved.append(g)
            del self.clusters[g]
        self._drop_guests(moved)
        return name, len(new), moved

    # --- a speaker corrected on the admin page is a sample of that voice ---
    def add_corrected(self, name, emb, heard=None, audio=None, utterance_id=None):
        """A phrase taken for `heard` that is `name`'s, says a person: into
        `name`'s voice the way an enrollment does, trusted; the copies auto-
        learning made of it under the wrong voice go. A 3-year-old will not do
        «запомни мой голос»: her voice is learnt from the phrases marked as hers.
        -> (profile, samples, merged clusters), or None for a guest."""
        e = _unit(emb).round(6).tolist()
        with self.lock:
            prof = self._profile_for(name)
            if not prof:
                return None
            if heard:
                wrong = next((p for p in self.profiles if self.same_person(p, heard)), heard)
                if wrong != prof and self.auto.get(wrong):
                    gone = [(i, a) for i, a in self.auto[wrong] if float(np.dot(e, a)) >= 0.9]
                    if gone:
                        self.auto[wrong] = [x for x in self.auto[wrong] if x not in gone]
                        self._exec("DELETE FROM voice_prints WHERE id = ANY(%s)", ([i for i, _ in gone if i],))
            return self._enroll(prof, [e], "correction", trusted=True,
                                meta=[{"audio": audio, "utterance_id": utterance_id}])

    # --- for the ear (its split check, till it moves here too) ---
    def export(self):
        with self.lock:
            return {"profiles": {k: list(v) for k, v in self.profiles.items()}, "self": list(self.self_)}

    def names(self):
        with self.lock:
            return sorted(self.profiles)
