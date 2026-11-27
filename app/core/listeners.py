"""Listeners: rules over every phrase the ear logs, not only what is said to her
(the swearing near the children, a stranger's voice at night...). Edited on the
admin page (services/admin), kept in

    /var/lib/eva/listeners.json   {"listeners": [...]}

A listener, checked from the cheapest step on, so that almost every phrase
costs nothing:

    {"id", "name", "enabled",
     "on":       "known" | "unknown" | "any"     whose voice: a family voice or not
     "who":      "any" | "adult" (not a child of the family: a guest too) | "child" (a family child)
                 | a voice profile of the family ("Лиза"; its label or how she calls them)
     "guess":    true - also a voice the base left unsigned (one short word) whose nearest
                 family voice is that person, sim >= GUESS_SIM (default true)
     "words":    a regular expression over the phrase ("" - every phrase)
     "children_within": N  - fires only if a child's voice was heard N s around it
     "prompt":   a yes/no question for the local model (router's Qwen, on the hub's GPU,
                 free): asked only after the steps above passed ("" - not asked)
     "action":   "say"       - text aloud at home (no Claude)
                 "telegram"  - text to `to` (a family name) in Telegram (no Claude)
                 "eva"       - text as an instruction to her: a command of the router's
                               catalog is done locally (free); otherwise a turn of the voice
                               conversation (Claude), with the phrase and its context
                 "log"       - only the log line
     "text", "to", "cooldown": s between two firings,
     "emotion":  a run of an emotion instead of one phrase (absent/null - a phrase listener):
                 {"label": "angry"|"sad"|"positive"|"neutral",
                  "window":    s looked back (300),
                  "min_count": phrases of that emotion at least (3),
                  "share":     their share of the phrases with an emotion in the window (0.6),
                  "min_score": the model's score a phrase needs to count (0.5),
                  "neutral":   neutral phrases count in the share (true) or are left out}
                 The emotion comes from the hub 1-2 s after its phrase (GigaAM-Emo, voices >= 2 s).
                 "on", "who", "words" pick which phrases count; "children_within", "prompt",
                 "cooldown" apply at the firing; a firing starts a new window.}

{phrase}, {who}, {children} in "text" are filled in; an emotion listener also has
{emotion}, {count}, {total}, {minutes} ({phrase} - the phrases of the run).
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
# a short word is left "unknown" by the voice base (services/stt/voices.py SHORT_SEC): its
# nearest family voice at least this close is taken as a guess (the base's "another room" level)
GUESS_SIM = 0.42
# the obscene roots at a word's start (STT writes them in full); editable on the admin page
SWEAR = (r"(?<![а-яё])([оа]?курв|kurw|ху[йеёяи]|пизд|бля|муда|мудил|пид[оа]р|залуп|гандон|долбо[её]б|ублюд|шлюх|"
         r"[её]б[аилнуыё]|въеб|заеб|уеб|выеб|отъеб|наеб|поеб|разъеб|охуе|нахуй|похуй|сука|суки|сучк)")
DEFAULTS = [{
    "id": "swearing", "name": "Ругань при детях", "enabled": True,
    "on": "any", "who": "adult", "words": SWEAR, "children_within": 120, "prompt": "",
    "action": "say", "text": "Пожалуйста, не ругайтесь при детях.", "to": "", "cooldown": 600,
}]

RECENT = collections.deque()                  # (ts, name|None, known, child, text, label)
EMO_KEEP = 3600                               # s of phrase emotions kept (the longest window)
EMO_RU = {"angry": "злость", "sad": "грусть", "positive": "радость", "neutral": "спокойствие"}
EMOS = collections.deque()                    # dicts: a phrase (ts, text, name, ...) + emo, score
_by_audio = {}                                # clip -> the phrase, waiting for its emotion
_early = {}                                   # clip -> (ts, emotion) come before its phrase
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


def feed(item, name, known, child, label=None, near=None):
    """Every logged phrase comes here (dialog.on_phrase); checked in a thread of
    its own so that the dialog never waits for a listener."""
    try:
        _q.put_nowait(("p", time.time(), item.get("text", "").strip(), name, known, child, label, near,
                       (item.get("audio") or "").split("#")[0] or None))
    except queue.Full:
        pass


def claims(text, name, known, child, label=None, near=None):
    """Will a music listener act on this phrase by its words alone (no model question, no emotion,
    no children check)? Then the dialog leaves it: the listener plays the track, Claude is not paid
    for the same words (2026-11-27 17:51: «включи сборку» played and also went to Sonnet)."""
    for x in load():
        if (x.get("enabled") and x.get("action") == "music" and x.get("_re") is not None and not x.get("emotion")
                and not (x.get("prompt") or "").strip() and not x.get("children_within")
                and _whose(x, text.strip(), name, known, child, label, near)):
            # in its pause it will not play - then the phrase is the dialog's; "fired a moment ago"
            # is this very phrase (the listener's thread may be first)
            ago = time.time() - _state["fired"].get(x["id"], 0)
            if ago >= float(x.get("cooldown") or 0) or ago < 5:
                return True
    return False


def emotion(ev):
    """A phrase's speech emotion from the hub (hub_ear): {"audio", "label", "score", "probs"}."""
    try:
        _q.put_nowait(("e", time.time(), ev))
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


def _guess(near):
    return near if near and near.get("sim", 0) >= GUESS_SIM else None


def _whose(x, text, name, known, child, label=None, near=None):
    """The voice and the words: does this phrase concern the listener at all."""
    on = x.get("on", "any")
    if (on == "known" and not known) or (on == "unknown" and known):
        return False
    who = x.get("who", "any")
    if (who == "child" and not child) or (who == "adult" and child):
        return False
    if who not in ("any", "adult", "child"):
        me = who.lower()
        if known:
            if me not in {(name or "").lower(), (label or "").lower()}:
                return False                  # one person: only their voice
        else:
            g = _guess(near) if x.get("guess", True) else None
            if not (g and me in {g["name"].lower(), g["label"].lower()}):
                return False                  # "adult": anyone but a child of the family (a guest too)
    if x.get("_re") is not None and not x["_re"].search(text):
        return False
    return True


def _match(x, ts, text, name, known, child, label=None, near=None):
    """-> (fire?, children near) ; the cheap steps first."""
    if not _whose(x, text, name, known, child, label, near):
        return False, []
    kids = []
    if x.get("children_within"):
        kids = _children_near(ts, float(x["children_within"]))
        if not kids:
            return None, []
    return True, kids


def _act(x, ts, text, name, kids, extra=None, why=None):
    """`extra`: more {placeholders}; `why`: what fired it, when not one phrase (a run of emotion)."""
    who = name or "неизвестный голос"
    msg = (x.get("text") or "").replace("{phrase}", text).replace("{who}", who) \
        .replace("{children}", ", ".join(kids))
    for k, v in (extra or {}).items():
        msg = msg.replace("{" + k + "}", str(v))
    action = x.get("action", "log")
    log(f"listener {x['id']}: fired on {who}: " + (f"{why}: " if why else "") + f"{text!r}"
        + (f" (children: {', '.join(kids)})" if kids else ""))
    heard = f"{why}: {text}" if why else f"«{text}»"
    # every firing ends in a status line "listener <id>: ok: ..." / "error: ..." (the admin page
    # shows it): «давайте жить дружно» fired three times and nothing said the song never played
    # (2026-11-27). Music reports from the player, when the sound starts or the track fails.
    status = lambda ok, what: log(f"listener {x['id']}: {'ok' if ok else 'error'}: {what}")
    if action == "say" and msg:
        import dialog
        dialog.VOICE.say(msg)
        dialog.history.append({"role": "assistant", "content": f"[слушатель «{x.get('name')}»: услышала "
                                                               f"{heard} ({who})] {msg}"})
        dialog.state["last_spoke"] = time.time()
        dialog.save_history()
        status(True, f"said {msg!r}")
    elif action == "telegram" and msg and x.get("to"):
        from skills.home import send
        from core.identity import Person
        try:
            out = send({"name": x["to"], "text": msg}, Person("Ева"))
            status(True, f"telegram to {x['to']}: {str(out)[:150]}")
        except Exception as e:
            status(False, f"telegram to {x['to']}: {e}")
    elif action == "music" and msg:
        # exactly the query written, no interpretation, no Claude: «Наутилус - Казанова»
        import dialog
        from core.identity import Person
        dialog.WORK.put((f"[слушатель «{x.get('name')}»] включи {msg}", Person("Ева", channel="voice"),
                         {"intent": "music_play", "query": msg, "kind": "track", "listener": x["id"],
                          # the name chosen on the admin page: a link's own name is "watch" until /info
                          # answers (the log and the screen said 'watch', 2026-11-27 16:11)
                          "title": x.get("track") if x.get("trackUrl") == msg.strip() else None}))
    elif action == "log" or not msg:
        status(True, "logged")
    elif action == "eva" and msg:
        import dialog
        from core.identity import Person
        # the model ladder (core/llm.py): a command from the router's catalog («включи
        # Наутилус», «громче», «распечатай это») is done locally, free; Claude only for the rest
        r = router.route(msg, named=True, in_dialog=True)
        if r and r.get("intent") not in (None, "claude", "ignore", "unclear", "ack"):
            log(f"listener {x['id']}: by the router: {r['intent']} {({k: v for k, v in r.items() if k not in ('intent', 'ms')})}")
            dialog.WORK.put((f"[слушатель «{x.get('name')}»] {msg}", Person("Ева", channel="voice"),
                             dict(r, listener=x["id"])))
            if r["intent"] != "music_play":          # music reports itself when it sounds
                status(True, f"done locally: {r['intent']}")
            return
        dialog.WORK.put((msg, Person("Ева", channel="voice"), None,
                         f"[система: это сработал слушатель «{x.get('name')}». Услышано: {heard} "
                         f"({who}). Недавно дома говорили:\n{_context(ts)}\nСделай, что сказано; "
                         f"если делать нечего - ответь <молчу>]"))
        status(True, "passed to Eva (Claude)")


def _try(x, ts, text, name, known, child, label=None, near=None, waited=False):
    ok, kids = _match(x, ts, text, name, known, child, label, near)
    if ok is None:
        if not waited:
            # no child heard yet: a child may answer right after - look again a bit later
            _later.append((time.time() + float(x["children_within"]) / 4, ts, text, name, known, child, label, near, x["id"]))
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
    if not known and _guess(near) and x.get("who") not in ("any", "adult", "child", None):
        name = f"{_guess(near)['name']} (вероятно, {near['sim']:.2f})"
    try:
        _act(x, ts, text, name, kids)
    except Exception as e:
        log(f"listener {x['id']}: error: {e!r}")


def _check(ts, text, name, known, child, label, near):
    for x in load():
        if x.get("enabled") and not x.get("emotion"):
            _try(x, ts, text, name, known, child, label, near)


def _emo_cfg(x):
    e = x.get("emotion") or {}
    return (e.get("label") or "angry", float(e.get("window") or 300), max(1, int(e.get("min_count") or 3)),
            float(e.get("share") if e.get("share") is not None else 0.6),
            float(e.get("min_score") if e.get("min_score") is not None else 0.5), e.get("neutral", True) is not False)


def _run_of(x, now):
    """-> (the phrases of the run, all the phrases weighed) when the emotion prevails, else None.
    Counted since the window's start or the last firing, whichever is later."""
    lab, win, mn, share, ms, neutral = _emo_cfg(x)
    since = max(now - win, _state["fired"].get(x["id"], 0))
    pool = [p for p in EMOS if p["ts"] >= since and (neutral or p["emo"] != "neutral" or lab == "neutral")
            and _whose(x, p["text"], p["name"], p["known"], p["child"], p["label"], p["near"])]
    hits = [p for p in pool if p["emo"] == lab and p["score"] >= ms]
    if len(hits) < mn or len(hits) < share * len(pool):
        return None
    return hits, pool


def _try_emo(x, now):
    found = _run_of(x, now)
    if not found or now - _state["fired"].get(x["id"], 0) < float(x.get("cooldown") or 0):
        return
    hits, pool = found
    lab, win = _emo_cfg(x)[:2]
    kids = []
    if x.get("children_within"):
        kids = _children_near(now, float(x["children_within"]))
        if not kids:
            return
    st = STATS.setdefault(x["id"], {"checked": 0, "fired": 0, "last": None})
    st["checked"] += 1
    text = " / ".join(p["text"] for p in hits[-6:])
    who = ", ".join(dict.fromkeys(p["name"] or "неизвестный голос" for p in hits))
    why = f"{EMO_RU.get(lab, lab)} в {len(hits)} из {len(pool)} фраз за {win / 60:g} мин"
    if x.get("prompt") and not _ask(x["prompt"], text, who, _context(now, around=win)):
        log(f"listener {x['id']}: the local model said no to {why}")
        return
    _state["fired"][x["id"]] = now
    st["fired"] += 1
    st["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _act(x, now, text, who, kids, why=why,
             extra={"emotion": EMO_RU.get(lab, lab), "count": len(hits), "total": len(pool), "minutes": f"{win / 60:g}"})
    except Exception as e:
        log(f"listener {x['id']}: error: {e!r}")


def _emo_add(p, ev, now):
    p = dict(p, emo=ev.get("label"), score=float(ev.get("score") or 0))
    EMOS.append(p)
    while EMOS and now - EMOS[0]["ts"] > EMO_KEEP:
        EMOS.popleft()
    for x in load():
        if x.get("enabled") and x.get("emotion"):
            _try_emo(x, now)


def _on_emotion(now, ev):
    a = ev.get("audio")
    if not a:
        return
    p = _by_audio.pop(a, None)
    if p:
        _emo_add(p, ev, now)
    else:
        _early[a] = (now, ev)                 # the phrase is still on its way (or dropped: TV, noise)


_later = []                                   # phrases waiting for a child's voice after them


def _on_phrase(ts, text, name, known, child, label, near, audio):
    g = _guess(near)
    # a child's short word left unsigned still counts as "a child near" when its voice is nearest
    RECENT.append((ts, name or (g["name"] if g else None), known, child or bool(g and g.get("child")), text, label))
    while RECENT and ts - RECENT[0][0] > KEEP:
        RECENT.popleft()
    if audio and audio not in _by_audio:
        # waits for its emotion (a split phrase: the first part takes the clip's emotion)
        p = {"ts": ts, "text": text, "name": name, "known": known, "child": child, "label": label, "near": near}
        if audio in _early:
            _emo_add(p, _early.pop(audio)[1], ts)
        else:
            _by_audio[audio] = p
    _check(ts, text, name, known, child, label, near)


def run():
    while True:
        try:
            m = _q.get(timeout=1)
            if m[0] == "e":
                _on_emotion(*m[1:])
            else:
                _on_phrase(*m[1:])
        except queue.Empty:
            pass
        except Exception as e:
            log(f"listeners: {e!r}")          # one bad event must not stop the thread
        now = time.time()
        for a in [a for a, p in _by_audio.items() if now - p["ts"] > 60]:
            del _by_audio[a]                  # a phrase too short for an emotion: none comes
        for a in [a for a, (t, _) in _early.items() if now - t > 60]:
            del _early[a]
        for w in [w for w in _later if w[0] <= now]:
            _later.remove(w)
            _, ts, text, name, known, child, label, near, xid = w
            x = next((x for x in load() if x["id"] == xid and x.get("enabled")), None)
            if x:
                _try(x, ts, text, name, known, child, label, near, waited=True)


def start():
    load()
    threading.Thread(target=run, daemon=True).start()
