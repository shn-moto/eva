"""The household profile: everything about THIS home that the code must not
hard-code. One file, eva/profile.json on the device (EVA_PROFILE overrides the
path), never in git; eva.example/profile.json shows the format. The persona
texts (BOOTSTRAP.md, LEGEND.md, PRIVATE.md, names.json) live next to it.

    household.children       names with no "adult" permissions (money, acting in the house)
    household.name_stems     [stem, name]: "я Иван" bent by the recogniser ("ля ивана") still finds it
    household.voice_call     voice profile label -> how Eva calls that person
    household.voice_person   voice profile label -> person id in the photolib family tree
    household.voice_aliases  voice profile label -> every name it answers to
    household.finance_users  login in the finance service -> the person's name
    speech.stt_fixes         [regex, replacement]: names the recogniser always mangles
    speech.hotwords          names to push up in recognition
    household.currency       what the finance service counts in ("PLN")
    household.document_tags  photolib tag ids whose photos are documents (hidden, private)
    skills                   the skills this home has (names of app/skills packages); absent = all
    tuning.<module.name>     any tuning value (see tune()): thresholds, windows, models, limits
    services.hub             the server that runs the heavy services (STT, router, music...)
    services.<name>          URL of one service; "{hub}" is replaced. An environment
                             variable EVA_<NAME>_URL overrides it.
"""
import json
import os

PATH = os.environ.get("EVA_PROFILE", "/opt/eva/eva/profile.json")


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_P = _load()
_H = _P.get("household", {})
_S = _P.get("speech", {})
_SV = _P.get("services", {})

CHILDREN = set(_H.get("children", []))
NAME_STEMS = [tuple(x) for x in _H.get("name_stems", [])]
VOICE_CALL = dict(_H.get("voice_call", {}))
VOICE_PERSON = {k: int(v) for k, v in _H.get("voice_person", {}).items()}
VOICE_ALIASES = {k: set(v) for k, v in _H.get("voice_aliases", {}).items()}
FINANCE_USERS = dict(_H.get("finance_users", {}))
CURRENCY = _H.get("currency", "")
SKILLS = set(_P["skills"]) if isinstance(_P.get("skills"), list) else None   # None: all skills in app/skills
DOCUMENT_TAGS = tuple(_H.get("document_tags", []))
# databases Eva may read (skill "data"): [{name, dsn: <secret key>, needs, about}]
DATABASES = list(_P.get("databases", []))
STT_FIXES = [tuple(x) for x in _S.get("stt_fixes", [])]
HOTWORDS = list(_S.get("hotwords", []))
HUB = os.environ.get("EVA_HUB", _SV.get("hub", "127.0.0.1"))

# the default port and path of each service; the host is the hub
DEFAULTS = {
    "stt": "http://{hub}:8901/stt",
    "gate": "http://{hub}:8902/v1/chat/completions",
    "memory": "http://{hub}:8903/search",
    "music": "http://{hub}:8904",
    "router": "http://{hub}:8906/v1/chat/completions",
    "photolib": "http://{hub}:8000",
    "finance": "http://{hub}:8686",
    "power": "http://{hub}:8484",
}


_T = _P.get("tuning", {})


def tune(key, default):
    """A tuning value: env EVA_<KEY> (dots -> _), else profile.json tuning.<key>,
    else `default` - the value measured here, kept in the code next to the
    reason for it. The type follows the default (a list for a tuple default
    becomes a tuple)."""
    env = os.environ.get("EVA_" + key.upper().replace(".", "_"))
    if env is None and key not in _T:
        return default
    v = env if env is not None else _T[key]
    if isinstance(default, bool):
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
    if isinstance(default, int):
        return int(float(v))
    if isinstance(default, float):
        return float(v)
    if isinstance(default, (tuple, list, set, frozenset)):
        return type(default)(json.loads(v) if isinstance(v, str) else v)
    return v


def service(name):
    """URL of a service: env EVA_<NAME>_URL, else the profile, else the default on the hub."""
    url = os.environ.get(f"EVA_{name.upper()}_URL") or _SV.get(name) or DEFAULTS[name]
    return url.replace("{hub}", HUB)
