"""Listeners: rules over every phrase the ear logs, not only what is said to her
(the swearing near the children, a stranger's voice at night...). Edited on the
admin page (services/admin), kept in

    /var/lib/eva/listeners.json   {"listeners": [...]}

A listener, checked from the cheapest step on, so that almost every phrase
costs nothing:

    {"id", "name", "enabled",
     "on":       "known" | "unknown" | "any"     whose voice: a family voice or not
     "who":      "any" | "adult" (not a child of the family: a guest too) | "child" (a family child)
                 | a voice profile of the family ("Аэлита"; its label or how she calls them)
     "words":    a regular expression over the phrase ("" - every phrase)
     "children_within": N  - fires only if a child's voice was heard N s around it
     "prompt":   a yes/no question for the local model (router's Qwen, on the hub's GPU,
                 free): asked only after the steps above passed ("" - not asked)
     "action":   "say"       - text aloud at home (no Claude)
                 "telegram"  - text to `to` (a family name) in Telegram (no Claude)
                 "eva"       - text as an instruction to her: a turn of the voice
                               conversation (Claude), with the phrase and its context
                 "log"       - only the log line
     "text", "to", "cooldown": s between two firings}

{phrase}, {who}, {children} in "text" are filled in.
"""
import collections
import json
import os
import queue
import re
import threading
import time
import urllib.request

from core import router
from core.util import log

FILE = os.environ.get("EVA_LISTENERS", "/var/lib/eva/listeners.json")
KEEP = 600                                    # s of phrases remembered for the context
# the obscene roots at a word's start (STT writes them in full); editable on the admin page
SWEAR = (r"(?<![а-яё])(ху[йеёяи]|пизд|бля|муда|мудил|пид[оа]р|залуп|гандон|долбо[её]б|ублюд|шлюх|"
         r"[её]б[аилнуыё]|въеб|заеб|уеб|выеб|отъеб|наеб|поеб|разъеб|охуе|нахуй|похуй|сука|суки|сучк)")
DEFAULTS = [{
    "id": "swearing", "name": "Ругань при детях", "enabled": True,
    "on": "any", "who": "adult", "words": SWEAR, "children_within": 120, "prompt": "",
    "action": "say", "text": "Пожалуйста, не ругайтесь при детях.", "to": "", "cooldown": 600,
}]

RECENT = collections.deque()                  # (ts, name|None, known, child, text, label)
_q = queue.Queue(maxsize=200)
_state = {"mtime": -1, "items": [], "fired": {}}       # -1: not read yet
STATS = {}                                    # id -> {"checked", "fired", "last"} (since the start)


def load():
    """The listeners, re-read when the file changes (the admin page writes it)."""
    try:
        m = os.path.getmtime(FILE)
    except OSError:
        m = None
    if m != _state["mtime"]:
        _state["mtime"] = m
        if m is None:
            # first start: the defaults, written out so the admin page shows them
            _state["items"] = [dict(x) for x in DEFAULTS]
            try:
                save(_state["items"])
            except OSError as e:
                log(f"listeners: not saved: {e}")
            _state["mtime"] = -1              # read back (with its regexes) on the next look
            return load() if os.path.exists(FILE) else _state["items"]
        else:
            try:
                with open(FILE, encoding="utf-8") as f:
                    _state["items"] = json.load(f).get("listeners", [])
                for x in _state["items"]:
                    x["_re"] = re.compile(x["words"], re.I) if x.get("words") else None
                log(f"listeners: {len([x for x in _state['items'] if x.get('enabled')])} on")
            except (OSError, ValueError, re.error) as e:
                log(f"listeners: bad file, kept the old ones: {e}")
    return _state["items"]


def save(items):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"listeners": [{k: v for k, v in x.items() if not k.startswith("_")} for x in items]},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def feed(item, name, known, child, label=None):
    """Every logged phrase comes here (dialog.on_phrase); checked in a thread of
    its own so that the dialog never waits for a listener."""
    try:
        _q.put_nowait((time.time(), item.get("text", "").strip(), name, known, child, label))
    except queue.Full:
        pass


def _ask(prompt, phrase, who, context):
    """A yes/no from the local model. None when it is not reachable."""
    body = {"messages": [
        {"role": "system", "content": "Ты проверяешь фразы, услышанные дома голосовым помощником. "
                                      "Фраза распознана из речи и может содержать ошибки. Ответь только да или нет."},
        {"role": "user", "content": f"Недавно дома говорили:\n{context}\n\nФраза ({who}): «{phrase}»\n\n"
                                    f"Вопрос: {prompt}"}],
        "temperature": 0, "max_tokens": 4}
    try:
        req = urllib.request.Request(router.URL, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=router.TIMEOUT + 2) as r:
            out = json.loads(r.read())["choices"][0]["message"]["content"].strip().lower()
    except Exception as e:
        log(f"listeners: local model: {e}")
        return None
    return out.startswith("да") or out.startswith("yes")


def _context(now, around=120):
    return "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(ts))} {n or 'неизвестный голос'}: {t}"
                     for ts, n, _, _, t, _ in RECENT if now - ts <= around)[-1500:]


def _children_near(now, within):
    return sorted({n for ts, n, _, child, _, _ in RECENT if child and abs(now - ts) <= within})


def _match(x, ts, text, name, known, child, label=None):
    """-> (fire?, children near) ; the cheap steps first."""
    on = x.get("on", "any")
    if (on == "known" and not known) or (on == "unknown" and known):
        return False, []
    who = x.get("who", "any")
    if (who == "child" and not child) or (who == "adult" and child):
        return False, []
    if who not in ("any", "adult", "child") and not (known and who.lower() in {(name or "").lower(), (label or "").lower()}):
        return False, []                      # one person: only their recognised voice                      # "adult": anyone but a child of the family (a guest too)
    if x.get("_re") is not None and not x["_re"].search(text):
        return False, []
    kids = []
    if x.get("children_within"):
        kids = _children_near(ts, float(x["children_within"]))
        if not kids:
            return None, []
    return True, kids


def _act(x, ts, text, name, kids):
    who = name or "неизвестный голос"
    msg = (x.get("text") or "").replace("{phrase}", text).replace("{who}", who) \
        .replace("{children}", ", ".join(kids))
    action = x.get("action", "log")
    log(f"listener {x['id']}: fired on {who}: {text!r}" + (f" (children: {', '.join(kids)})" if kids else ""))
    if action == "say" and msg:
        import dialog
        dialog.VOICE.say(msg)
        dialog.history.append({"role": "assistant", "content": f"[слушатель «{x.get('name')}»: услышала "
                                                               f"«{text}» ({who})] {msg}"})
        dialog.state["last_spoke"] = time.time()
        dialog.save_history()
    elif action == "telegram" and msg and x.get("to"):
        from skills.home import send
        from core.identity import Person
        try:
            send({"name": x["to"], "text": msg}, Person("Ева"))
        except Exception as e:
            log(f"listener {x['id']}: telegram: {e}")
    elif action == "eva" and msg:
        import dialog
        from core.identity import Person
        dialog.WORK.put((msg, Person("Ева", channel="voice"), None,
                         f"[система: это сработал слушатель «{x.get('name')}». Услышано: «{text}» "
                         f"({who}). Недавно дома говорили:\n{_context(ts)}\nСделай, что сказано; "
                         f"если делать нечего - ответь <молчу>]"))


def _try(x, ts, text, name, known, child, label=None, waited=False):
    ok, kids = _match(x, ts, text, name, known, child, label)
    if ok is None:
        if not waited:
            # no child heard yet: a child may answer right after - look again a bit later
            _later.append((time.time() + float(x["children_within"]) / 4, ts, text, name, known, child, label, x["id"]))
        return
    if not ok or time.time() - _state["fired"].get(x["id"], 0) < float(x.get("cooldown") or 0):
        return
    st = STATS.setdefault(x["id"], {"checked": 0, "fired": 0, "last": None})
    st["checked"] += 1
    if x.get("prompt"):
        if not _ask(x["prompt"], text, name or "неизвестный голос", _context(ts)):
            log(f"listener {x['id']}: the local model said no to {text!r}")
            return
    _state["fired"][x["id"]] = time.time()
    st["fired"] += 1
    st["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _act(x, ts, text, name, kids)
    except Exception as e:
        log(f"listener {x['id']}: {e}")


def _check(ts, text, name, known, child, label):
    for x in load():
        if x.get("enabled"):
            _try(x, ts, text, name, known, child, label)


_later = []                                   # phrases waiting for a child's voice after them


def run():
    while True:
        try:
            ts, text, name, known, child, label = _q.get(timeout=1)
            RECENT.append((ts, name, known, child, text, label))
            while RECENT and ts - RECENT[0][0] > KEEP:
                RECENT.popleft()
            _check(ts, text, name, known, child, label)
        except queue.Empty:
            pass
        now = time.time()
        for w in [w for w in _later if w[0] <= now]:
            _later.remove(w)
            _, ts, text, name, known, child, label, xid = w
            x = next((x for x in load() if x["id"] == xid and x.get("enabled")), None)
            if x:
                _try(x, ts, text, name, known, child, label, waited=True)


def start():
    load()
    threading.Thread(target=run, daemon=True).start()
