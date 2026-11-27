"""Who is speaking - asked of the hub. The voice base and every rule about it
live on shn-linux (services/stt/voices.py, Postgres); the Station only hears
(docs/EAR-PLAN.md, stage 1, 2026-11-24). Here: the client the brain calls with
the same names as before, and the household's names (its private profile),
sent to the hub at the start.
"""
import json
import threading
import time
import urllib.request

from core import profile
from core.util import log

HUB = profile.service("stt").rsplit("/", 1)[0] + "/voices"
HEAR_CONFIG = profile.service("stt").rsplit("/", 1)[0] + "/hear/config"
ID_MARGIN = profile.tune("voices.margin", 0.1)     # a claim's lead (the hub's rule, the same number)
# a known voice saying it is a family member with no voice yet is enrolled under
# that name unless it is this sure to be the voice it was taken for
ENROLL_KNOWN_MAX = profile.tune("voices.enroll_known_max", 0.75)
# profile names as stored -> how Eva calls them (the old profile is "Ivan")
CALL = profile.VOICE_CALL
# voice profile -> person in the photolib family tree (one Иван for voice and Telegram)
PERSON = profile.VOICE_PERSON
# every name a profile answers to: "я Ваня" from Ivan is not an impostor
ALIASES = profile.VOICE_ALIASES


def same_person(prof, name):
    """Does `name` (as said in an introduction) mean this profile?"""
    n = name.lower()
    return n == prof.lower() or n in ALIASES.get(prof, set()) or n == CALL.get(prof, "").lower()


def people():
    """The household's names for the hub: who is family, what each answers to."""
    names = set(CALL) | set(ALIASES) | {n for _, n in profile.NAME_STEMS} | set(profile.CHILDREN)
    return [{"name": n, "aliases": sorted(set(ALIASES.get(n, set())) | ({CALL[n].lower()} if n in CALL else set())),
             "family": True} for n in sorted(names)]


def _call(path, body=None, timeout=3):
    req = urllib.request.Request(path if path.startswith("http") else HUB + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Voices:
    """The hub's voice base, as the brain sees it. The hub away: nobody is
    recognised (a guest), nothing breaks."""

    def __init__(self):
        self._names, self._guests, self._at = [], 0, 0.0
        threading.Thread(target=self._send_people, daemon=True).start()

    def _send_people(self):
        """The household's names (voice base) and its name fixes (the hub's ear,
        services/stt/hear.py) - again every 10 minutes: a restarted hub keeps
        the names in its base but not the fixes."""
        sent = False
        while True:
            try:
                _call("/people", {"people": people()}, timeout=10)
                _call(HEAR_CONFIG, {"stt_fixes": [list(x) for x in profile.STT_FIXES]}, timeout=10)
                if not sent:
                    log(f"voices: household names and name fixes sent to the hub ({len(people())})")
                sent = True
                time.sleep(600)
            except Exception as e:
                log("voices: hub not ready:", e)
                time.sleep(20)

    def identify(self, emb, dur):
        """(label, similarity, known)"""
        try:
            r = _call("/identify", {"emb": list(emb), "dur": float(dur or 0)})
            return r["label"], r["sim"], r["known"]
        except Exception as e:
            log("voices: identify failed:", e)
            return "unknown", 0.0, False

    def claim(self, emb, name, lead=ID_MARGIN):
        try:
            r = _call("/claim", {"emb": list(emb), "name": name, "lead": lead})
            return r["profile"], r["sim"]
        except Exception as e:
            log("voices: claim failed:", e)
            return None, 0.0

    def start_enroll(self, name, seconds=60, first=None):
        try:
            _call("/enroll/start", {"name": name, "first": list(first) if first else None, "seconds": seconds})
        except Exception as e:
            log("voices: enroll start failed:", e)

    def _enroll(self):
        try:
            return _call("/enroll")
        except Exception:
            return {"name": None, "samples": 0}

    def enrolling(self):
        return self._enroll()["name"]

    def enroll_count(self):
        return self._enroll()["samples"]

    def finish_enroll(self):
        """-> (name, samples, merged clusters)"""
        try:
            r = _call("/enroll/finish", {}, timeout=10)
            return r["name"], r["samples"], r["merged"]
        except Exception as e:
            log("voices: enroll finish failed:", e)
            return None, 0, []

    def _refresh(self):
        if time.time() - self._at > 60:
            try:
                r = _call("/names")
                self._names, self._guests, self._at = r["names"], r["guests"], time.time()
            except Exception:
                pass

    def names(self):
        self._refresh()
        return list(self._names)

    def guests(self):
        self._refresh()
        return self._guests
