"""Family names in recognised speech (eva/names.json).

fix(): deterministic repair of the names STT mangles most - "Ева" at the start
of a phrase ("ево", "евро", "ега", "я вот") and the family's own names that come
out mangled every time (eva/profile.json speech.stt_fixes).
On the 2026-11-19 test set it cut GigaAM v3 errors 16.7% -> 13.6% and found
30 of 33 names instead of 24 (docs/stt-test). Pet forms ("Сонечка", "Сонька")
are real words and are left as they are: prompt_block() tells Eva who they are.
"""
import json
import re

from core import profile

NAMES = "/opt/eva/eva/names.json"

# only forms that are not ordinary words: "его", "я вот", "и вот" start plenty of
# household phrases and would make them look addressed to Eva (= Claude calls)
EVA_START = re.compile(r"^(?:ево|евро|ега|иво|ива|яро|е вот)\b\s*")
EVA_GLUED = re.compile(r"^(?:ево|ега|яро)(стоп|хватит)")
# the family's own names the recogniser always mangles: eva/profile.json speech.stt_fixes
FIXES = [(re.compile(rx), name) for rx, name in profile.STT_FIXES]


def fix(text):
    t = text.lower().replace("ё", "е").strip()
    if not re.match(r"^(?:ева|эва)\b", t):
        m = EVA_START.match(t)
        if m and t[m.end():]:
            t = "ева " + t[m.end():]
        else:
            m = EVA_GLUED.match(t)
            if m:
                t = "ева " + m.group(1) + t[m.end():]
    for rx, name in FIXES:
        t = rx.sub(name, t)
    return t


def prompt_block():
    try:
        data = json.load(open(NAMES, encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    lines = []
    for canon, v in data.items():
        if canon.startswith("_"):
            continue
        forms = [f if isinstance(f, str) else f"{f['form']} (так зовёт {f['by']})" for f in v["forms"] if f != canon]
        if forms:
            lines.append(f"- {canon}: {', '.join(forms)}")
    return "## Как в семье называют друг друга\n\n" + "\n".join(lines) if lines else ""
