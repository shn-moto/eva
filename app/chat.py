"""Text channels (Telegram): same Eva, same skills and memory, a conversation
of its own per person; the answer comes back whole, as text."""
import json
import os
import threading

import memory
from core import cost, llm
from core.prompt import SILENT, system
from core.util import log, ru_now

CHATS_FILE = "/var/lib/eva/chats.json"   # the chats survive a restart, like the voice conversation
LOCK = threading.Lock()


def _load():
    try:
        d = json.load(open(CHATS_FILE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save():
    tmp = CHATS_FILE + ".tmp"
    try:
        with LOCK:
            data = json.dumps(CHATS, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, CHATS_FILE)
    except OSError as e:
        log("chats save error:", e)


CHATS = _load()       # key -> [{"role", "content"}]


def reply(text, person, key):
    """-> (answer text, [presentations] for the channel to render)"""
    with LOCK:
        hist = CHATS.setdefault(key, [])
        hist.append({"role": "user", "content": f"[{ru_now()}] [{person.name}] {text}"})
        llm.trim(hist)
        msgs = list(hist)
    if person.known:
        memory.write("user", text, speaker=person.profile, dialog=True, addressed=True, stt="telegram")
    shows = []
    rounds = []                    # the real tool calls and results go into the history
    try:
        out = llm.complete(system("chat", person), msgs, person, on_show=shows.append, record=rounds)
    except cost.OverBudget as e:
        log("over the daily limit:", e)
        with LOCK:
            CHATS[key].pop()
        return "На сегодня я исчерпала лимит на умные ответы. Завтра снова буду на связи.", []
    out = out.replace(SILENT, "").strip()
    with LOCK:
        CHATS[key].extend(rounds)
    save()
    if person.known and out:
        memory.write("assistant", out)
    log(f"chat {person.name}: {text[:60]!r} -> {out[:80]!r}")
    return out, shows
