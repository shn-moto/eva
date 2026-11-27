"""The voice channel's conversation: phrases from ear.py -> is it for Eva? ->
answer, streamed, sentence by sentence to her voice. Interruptions (a stop or
her name over her own voice), "знакомство" (learning a voice), the case of
somebody else cutting in while she answers one person.
"""
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.request

import numpy as np

import memory
import music
import say
import voices
from core import cost, hub_ear, identity, listeners, llm, profile, router, screen
from core.prompt import SILENT, system
from core.speech import Voice, ui
from core.util import log, ru_now

GATE_URL = profile.service("gate")
DIALOG_WINDOW = profile.tune("dialog.window", 10.0)          # s after Eva last spoke: a phrase is likely for her (25 held on too long, 2026-11-19)
# The conversation survives a restart: two power-cut reboots on 2026-11-19 wiped
# it and she congratulated on the birthday again each time.
HISTORY_FILE = "/var/lib/eva/history.json"
# how much she keeps: llm.HISTORY_CHARS (by volume, not by time - 2026-11-20)
CONTINUE = "<продолжи>"
# said when a tool call begins and she has not said anything yet (sounds cached at start)
QUIET_TOOLS = {"music_play", "music_control"}   # done without a filler: the player shows it
FILLERS = profile.tune("dialog.fillers", ["Секунду, сейчас посмотрю.", "Сейчас гляну.", "Погоди, посмотрю.", "Так, сейчас посмотрю."])

# a call is the vocative: «почему ты назвал её Евой» is about her, not to her
# (Лиза asked Иван that and Eva answered, 2026-11-19)
NAME = re.compile(r"\b(ева|евочка|евушка|эва)\b", re.I)
# her name as it comes out over her own voice ("у ев покане" = "Ева, сколько…")
LOOSE_NAME = re.compile(r"(ева|евочка|евушка|эва|ев|эв|ив|иве|еве|еву|евы|евой)", re.I)
# how the recogniser mangles the name at the start of a phrase ("Ева, ты на
# связи?" came out as "е вот на связи")
# Not "и" / "э": the conjunction and the interjection start plenty of talk - 21 of
# 21 phrases in 4 days starting with "и" were not to her, and «и вырезаешь
# что-то» got "Не расслышала, повтори" (2026-11-23)
MAYBE_NAME = re.compile(r"^\s*(е|ив|иве|ева\w*|эва\w*|эв|ев)\b", re.I)
FUSED_STOP = re.compile(r"(?:е|э|и)в[аоы]?(?:сто[пк]\w*|хватит|выключи?\w*)", re.I)   # see music_command


def _music_cmd(text):
    """A word of a music command («стоп», «тише», «дальше»…) or a fused «евосток»."""
    w = re.findall(r"\w+", text.lower())
    return any(x in keys for x in w for keys, _ in MUSIC_WORDS) or any(FUSED_STOP.match(x) for x in w)


def _named_or_stop(text):
    """Her name (or its start-of-phrase mangling) or a fused «Ева, стоп» in a phrase."""
    w = re.findall(r"\w+", text.lower())
    return bool(NAME.search(text) or MAYBE_NAME.search(text) or (len(w) == 1 and FUSED_STOP.match(w[0])))
# a call on its own, the question comes after a pause ("Ева, … кто такая Ольга"
# arrived as "евро" + "кто такая ольга")
CALL_ONLY = re.compile(r"^\s*(?:эй\s+|слушай\s+)?(ева\w{0,3}|эва\w{0,2}|ев\w{0,2}|эв\w{0,2}|ив\w?|иве)\s*$", re.I)
CALL_WINDOW = profile.tune("dialog.call_window", 6.0)             # s after a call: the next phrase is for her
FOLLOW_WINDOW = profile.tune("dialog.follow_window", 10.0)   # s after asking: the same person going on is the same request
STOP = re.compile(r"\b(стоп|хватит|замолчи|молчи|тихо|тише|подожди|погоди|остановись|"
                  r"перестань|прекрати|отстань|достаточно|всё|все|пауза|заткнись)\b", re.I)
# everyday words that are a stop only said almost alone ("всё!", "тихо!"), not
# inside a longer phrase ("всё отлично" in her own echo cut her off, 2026-11-19)
WEAK_STOP = {"все", "всё", "тихо", "тише"}
# her own voice recognised in what the mics heard (ear.py: CAM++ vs her TTS
# voice): ignored while she talks - unless a family voice is in it too (a
# "стоп" said over her). Measured 2026-11-19: family voices 0.0-0.22, her
# clean TTS 0.72-1.0; through the room it is lower - to be tuned on the logs.
SELF_SIM, SELF_MARGIN = profile.tune("dialog.self_sim", 0.45), profile.tune("dialog.self_margin", 0.1)
# "запомни мой голос, я Маша" / "я Лиза, запомни мой голос" / "меня зовут Миша, запомни голос"
ENROLL = re.compile(r"запомни\s+(?:мой\s+)?голос\w*(?:\s+как|\s*,?\s*я|\s*,?\s*это)?\s+([а-яёa-z]+)", re.I)
INTRO = re.compile(r"(?:\bя\s+|меня\s+зовут\s+|это\s+)([а-яё]+)\b.*запомни\s+(?:мой\s+)?голос", re.I)
# family names by the stem: the recogniser bends them ("я Иван" -> "ля ивана")
FAMILY = profile.NAME_STEMS                 # eva/profile.json
NOT_A_NAME = {"тебя", "твой", "мой", "тоже", "хочу", "просто", "сейчас", "вот", "не", "ещё", "еще", "ля", "ну", "а", "и"}

VOICE = Voice()
VOICES = voices.Voices()
WORK = queue.Queue()
history = []                  # [{"role", "content"}], text only
REPLIES = {}                  # gen -> its history entry, to cut it down if she is stopped
RESTORED = {"note": False}    # the first answer after a restore gets a note about it
OWNER = {"person": identity.GUEST}   # whom she is answering now
VOICE_SEEN = {}                      # profile -> ts of the newest Telegram line already shown (memory.recent)
state = {"last_spoke": 0.0, "called_until": 0.0}
HEARD = []                    # (time, foreign words) heard over her voice, last few seconds
# After an interruption she is quiet and listens: everything heard now is his,
# no echo rules, no second "interruption" on her name. The answer goes out when
# his phrase ends (or LISTEN_IDLE after the last thing heard).
LISTEN = {"active": False, "parts": [], "deadline": 0.0}
LISTEN_LOCK = threading.Lock()
LISTEN_IDLE = profile.tune("dialog.listen_idle", 2.5)


def intro_name(text, m):
    """The name in an introduction: a family name anywhere in it first, else the
    word the pattern caught."""
    for w in re.findall(r"\w+", text.lower()):
        for stem, name in FAMILY:
            if w.startswith(stem):
                return name
    return m.group(1).capitalize() if m and m.group(1).lower() not in NOT_A_NAME else None


def gate(text):
    """Local model (shn-linux): is this phrase meant for a home assistant at all?
    Keeps household talk away from the cloud. False on error: a busy shn-linux
    let Маша's talk from the corridor through to Claude (2026-11-19); a phrase
    that really is for Eva names her or continues the conversation anyway."""
    t = time.time()
    body = {"messages": [
        {"role": "system", "content": "Ты фильтр голосового помощника в семье. Тебе дают фразу, "
         "услышанную в комнате. Ответь одним словом: ДА - если это вопрос, просьба или обращение "
         "к помощнику; НЕТ - если люди говорят между собой, это обрывок, шум или телевизор."},
        {"role": "user", "content": text}], "max_tokens": 3, "temperature": 0}
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            GATE_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=2)
        ans = json.loads(r.read())["choices"][0]["message"]["content"].strip().upper()
        log(f"gate {int(1000 * (time.time() - t))}ms: {ans}")
        return not ans.startswith("НЕТ")
    except Exception as e:
        log("gate error:", e)
        return False


# ------------------------------------------------------------------ history --
def save_history():
    """The context to disk after every change (small file, atomic replace)."""
    tmp = HISTORY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved": time.time(), "history": history}, f, ensure_ascii=False)
        os.replace(tmp, HISTORY_FILE)
    except OSError as e:
        log("history save error:", e)


def load_history():
    """After a restart the conversation comes back, as much as fits the budget
    (each phrase has its date and time - she sees how old it is)."""
    try:
        d = json.load(open(HISTORY_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    h = [m for m in d.get("history", []) if m.get("role") in ("user", "assistant") and m.get("content")]
    while h and h[-1]["role"] == "user":  # a question (or a tool result) left without an answer
        h.pop()
    history[:] = h
    llm.trim(history)
    RESTORED["note"] = bool(history)
    return len(history)


# --------------------------------------------------------------- answering --
def answer(text, person, owner=None, note=None):
    """owner: the Person she was answering when `person` cut in - she asks
    `person` to wait, finishes for the owner, then answers `person`."""
    now = ru_now()
    who = person.name
    if text == CONTINUE:
        content = f"[{now}] [система: тебя перебили, но не тот, кому ты отвечала. Договори ответ с того места, где остановилась.]"
    else:
        content = f"[{now}] [{who or 'голос не узнан'}] {text}"
    if note:
        content += "\n" + note
    if person.known:
        # what this person and she said in Telegram meanwhile: from the common log in the base
        tg, VOICE_SEEN[person.profile] = memory.recent(person.profile, "voice", VOICE_SEEN.get(person.profile), who)
        if tg:
            content += "\n" + tg
    import slideshow
    if screen.state["p"] is not None or music.PLAYER.active() or slideshow.describe():
        content += f"\n[экран: {screen.describe()}]"
    if owner:
        content += (f"\n[система: {who} перебил(а) тебя, пока ты отвечала {owner.name}. Сначала одной "
                    f"короткой фразой попроси {who} подождать (например «Подожди, {who}, я ещё не "
                    f"закончила отвечать …» - назови {owner.name} так, как зовёт его/её {who}), потом "
                    f"договори ответ {owner.name} с того места, где тебя перебили, и только потом "
                    f"ответь {who}.]")
    else:
        OWNER["person"] = person
    if RESTORED["note"]:
        RESTORED["note"] = False
        content += ("\n[система: колонка перезапускалась, разговор выше - до перезапуска, ты его помнишь. "
                    "Уже сказанное (поздравления, приветствия) не повторяй.]")
    history.append({"role": "user", "content": content})
    llm.trim(history)
    save_history()
    gen0 = VOICE.gen
    st = {"buf": "", "spoken": []}

    def on_chunk(chunk):
        if VOICE.gen != gen0:              # stopped mid-answer: stop writing it too
            return False
        st["buf"] += chunk
        b = st["buf"].strip()
        if SILENT.startswith(b[:len(SILENT)]) and len(b) <= len(SILENT):
            return True                    # could still become <молчу>
        while True:                        # sentence boundary: to the voice right away
            m = re.search(r"[.!?…]+[\")»]*\s", st["buf"])
            if not m:
                return True
            sent, st["buf"] = st["buf"][:m.end()].strip(), st["buf"][m.end():]
            if sent:
                if not st["spoken"] and text != CONTINUE:
                    ui({"type": "chat", "who": "me", "text": text})
                st["spoken"].append(sent)
                VOICE.say(sent, gen0)

    def on_tool(name):
        """A tool call begins: what she wrote before it goes out now, or - if
        she has said nothing yet - a short "секунду, посмотрю" (its sound is
        cached), so the room does not wait in silence for the tool round."""
        if VOICE.gen != gen0:
            return
        sent, st["buf"] = st["buf"].strip(), ""
        if sent.startswith(SILENT):
            sent = ""
        if not sent and (st["spoken"] or name in QUIET_TOOLS):
            return
        sent = sent or random.choice(FILLERS)
        if not st["spoken"] and text != CONTINUE:
            ui({"type": "chat", "who": "me", "text": text})
        st["spoken"].append(sent)
        VOICE.say(sent, gen0)
        log(f"tool {name}: said {sent!r} first")

    t0 = time.time()
    try:
        rounds = []                    # her tool calls and their results, for the history
        r = llm.stream(system("voice", person), history, person, on_chunk, on_show=show, on_tool=on_tool,
                       record=rounds)
    except cost.PaidOff as e:
        history.pop()
        log(f"not to Claude: {e}: {text!r}")
        # silent (her voice is a paid call too); the screen says why nothing happens
        ui({"type": "hear", "state": "ignored", "text": text + "  — платный API выключен"})
        return
    except cost.OverBudget as e:
        history.pop()                  # not answered: not in the conversation
        log("over the daily limit:", e)
        if state.get("limit_said") != time.strftime("%Y-%m-%d"):
            state["limit_said"] = time.strftime("%Y-%m-%d")
            VOICE.say("На сегодня я исчерпала лимит на умные ответы. Завтра снова буду на связи.")
        ui({"type": "hear", "state": "ignored", "text": text})
        return
    except llm.anthropic.APIError as e:
        log("claude error:", e)
        return
    if r["cut"]:
        # interrupted() has already put what she managed to say into history
        log(f"reply cut while writing ({len(st['spoken'])} sentences queued)")
        return
    rest = st["buf"].strip()
    if rest == SILENT or (not st["spoken"] and rest.startswith(SILENT)):
        if rounds:
            # done without a word (music on: the player is on the TV) - the calls stay in the
            # history, else she does not know she did it and does it again (2026-11-26)
            history.extend(rounds)
            history.append({"role": "assistant", "content": "(сделано)"})
            llm.trim(history)
            ui({"type": "hear", "state": "called", "text": text})
        else:
            history.pop()
            ui({"type": "hear", "state": "ignored", "text": text})
        save_history()
        log(f"silent ({int(1000 * (time.time() - t0))}ms)")
        return
    if rest:
        if not st["spoken"] and text != CONTINUE:
            ui({"type": "chat", "who": "me", "text": text})
        st["spoken"].append(rest)
        VOICE.say(rest, gen0)
    reply = " ".join(st["spoken"])
    if VOICE.gen != gen0:
        log("reply interrupted before it was recorded")
        return
    # the history gets the real tool calls and results, then her last words
    # (a note about them in her own text she did not believe, 2026-11-19)
    history.extend(rounds)
    entry = {"role": "assistant", "content": (llm.final_text(r["final"]) if rounds else "") or reply}
    history.append(entry)
    REPLIES[gen0] = entry
    save_history()
    state["last_spoke"] = state["last_answer"] = time.time()
    ui({"type": "chat", "who": "eva", "text": reply})
    if person.known:
        memory.write("assistant", say.untag(reply), speaker=person.profile, stt="voice")
    u = r["final"].usage
    log(f"reply: first token {int(1000 * (r['first'] or 0))}ms, "
        f"cache read {u.cache_read_input_tokens} / written {u.cache_creation_input_tokens}, "
        f"stop {r['final'].stop_reason}: {reply!r}")


# music commands are not confirmed aloud: the player on the TV shows it, and every spoken
# «Включаю …» costs a TTS call and lies over the first seconds of the music (user, 2026-11-26)
LOCAL_SAY = {"unclear": "Не расслышала. Повтори, пожалуйста.", "music_stop": None,
             "music_pause": None, "music_resume": None, "screen_close": None}
AVATAR_SAY = {"ascii": "Лицо из символов.", "image": "Аниме лицо.", "eq": "Эквалайзер.", "live2d": "Живая модель."}


class _Done(Exception):
    """A local branch finished early (its result and reply are set)."""


def local(text, person, r):
    """A command from the router's catalog, done without Claude. Whatever it
    says goes into the history too, so Claude knows it later."""
    intent, p = r["intent"], music.PLAYER
    reply = LOCAL_SAY.get(intent, "")
    result = None                       # what the action itself returned (print_screen: the job)
    try:
        if intent == "ack":
            reply = "Пожалуйста!" if re.search(r"спасиб", text, re.I) else None
        elif intent == "music_play":
            query = (r.get("query") or "").strip()
            if not query:
                # «Ева, включи музыку» - nothing to search for: the whole phrase went to the search
                # and «Винтаж - Ева я любила тебя» played (2026-11-23); then Claude asked «какую?»
                # for $0.19 (2026-11-27). Now the home's own music library opens - pick by number.
                import library
                out = library.browse({"kind": "music"}, person)
                if getattr(out, "show", None):
                    screen.present(out.show)
                result = getattr(out, "text", out)
                r["_call"] = ("library_browse", {"kind": "music"})
                reply = None if getattr(out, "show", None) else result
                raise _Done()
            kind = r.get("kind") or "track"
            found = music.search(query, "album" if kind == "album" else "track",
                                 {"track": 1, "selection": 8, "album": 1}.get(kind, 1))
            if not found:
                reply = f"Не нашла ничего по запросу «{query}»."
                if r.get("listener"):
                    log(f"listener {r['listener']}: error: nothing found for {query!r}")
            else:
                if r.get("listener"):
                    found[0]["listener"] = r["listener"]      # music.listener_log: ok on the first sound
                    # a listener wants that song or nothing, never a stand-in: «Живите дружно»
                    # played instead of the Leopold song (2026-11-27)
                    found[0].pop("alts", None)
                    if r.get("title") and len(found) == 1:
                        found[0]["title"] = r["title"]
                p.play(found)
                t = found[0]
                # silent: the history still learns what plays (the call below)
                result = (f"Включила {music.clean(t.get('title'))}"
                          + (f", {music.clean(t['artist'])}" if t.get("artist") else "") + ".")
                reply = None
        elif intent == "music_stop":
            alarm = sys.modules.get("skills.calendar")
            if alarm and alarm.ALARM["on"]:
                h = time.localtime().tm_hour
                reply = "Доброе утро!" if 4 <= h < 12 else "Будильник выключила."
            p.stop()
        elif intent in ("music_pause", "music_resume"):
            p.pause(intent == "music_pause")
        elif intent in ("music_next", "music_prev"):
            reply = None if p.next(1 if intent == "music_next" else -1) else "Дальше в очереди ничего нет."
        elif intent == "what_playing":
            t = p.now()
            reply = (f"Играет {t.get('title')}" + (f", {t['artist']}" if t.get("artist") else "") + ".") if t                 else "Сейчас ничего не играет."
        elif intent in ("volume_up", "volume_down"):
            p.louder(intent == "volume_up")
            reply = None
        elif intent == "volume_set":
            from core.speech import volume_set
            volume_set(max(5, min(100, int(r.get("level") or 50))))
            reply = None
        elif intent in ("library_open", "library_find") or (
                (screen.state.get("p") or {}).get("kind") == "library"
                and (intent in ("screen_open", "screen_back")
                     # «назад» on the first page is the folder above (the router says screen_prev)
                     or (intent == "screen_prev" and not screen.state.get("page")))):
            # the media library without Claude: open it, a folder by its number, up; a file plays
            # (Sonnet opened it and «открой первую» did nothing, 2026-11-27)
            import library
            if intent == "library_open" or (intent == "library_find" and not (r.get("query") or "").strip()):
                # library_find without the title (the router dropped it): the library itself
                call = ("library_browse", {"kind": r.get("lib") or "video"})
            elif intent == "library_find" and (r.get("query") or "").strip():
                call = ("library_browse", {"kind": r.get("lib") or "video", "query": r.get("query") or ""})
            elif intent in ("screen_back", "screen_prev"):
                call = ("library_browse", {"up": True})
            else:
                n = int(r.get("n") or 0)
                items = screen.state["p"].get("items") or []
                if not 1 <= n <= len(items):
                    raise ValueError(f"no tile {n}")
                call = ("library_browse" if items[n - 1]["folder"] else "library_play", {"n": n})
            out = (library.browse if call[0] == "library_browse" else library.play)(call[1], person)
            if getattr(out, "show", None):
                screen.present(out.show)
            result = getattr(out, "text", out)
            r["_call"] = call
            # the tiles say it; a film / a track starts silently; a failure is told
            reply = None if (getattr(out, "show", None) or result.startswith("Включила")) else result
        elif intent == "video_control":
            import library
            r["_call"] = ("video_control", {"action": r.get("action") or "pause"})
            result = library.control(r["_call"][1], person)
            reply = "Видео не играет." if "не играет" in result else None
        elif intent.startswith("screen_"):
            screen.act(intent[7:], r.get("n"))          # next | prev | open | back | close
            reply = None
        elif intent == "avatar":
            mode = r.get("mode") or "ascii"
            ui({"type": "avatar", "mode": mode})
            reply = AVATAR_SAY.get(mode)
        elif intent == "panel":
            from skills.screen import panel
            if r.get("panel") not in ("chat", "stats"):
                raise ValueError("panel?")
            panel({"panel": r["panel"], "show": r.get("show", True)}, person)
            reply = None
        elif intent == "time":
            reply = f"Сейчас {ru_now()}."
        elif intent == "print_screen":
            # done here, without Claude: printing is not a "smart answer" and works
            # past the day's limit too (2026-11-25)
            from importlib import import_module
            if not person.may("screen"):
                raise PermissionError("screen")
            out = result = import_module("skills.print").print_screen({}, person)
            reply = "Печатаю." if out.startswith("Отправила") else out
    except _Done:
        pass
    except Exception as e:
        log(f"local {intent} failed: {e} - to Claude")
        if r.get("listener"):
            log(f"listener {r['listener']}: error: {intent}: {e} - passed to Claude")
        answer(text, person)
        return
    log(f"local {intent}: {reply!r}")
    ui({"type": "chat", "who": "me", "text": text})
    if reply:
        VOICE.say(reply)
        ui({"type": "chat", "who": "eva", "text": reply})
        state["last_spoke"] = state["last_answer"] = time.time()
    else:
        ui({"type": "hear", "state": "called", "text": text})
    if intent != "unclear":
        history.append({"role": "user", "content": f"[{ru_now()}] [{person.name or 'голос не узнан'}] {text}"})
        call = _local_call(intent, r, result, reply)
        if call:
            # the action as a real call with its result, as if she had made it herself: with
            # only her word «Печатаю.» in the history she took it for a promise she had not
            # kept, and printed the same page again (2026-11-26 09:42)
            tid = f"toolu_local{int(time.time() * 1000)}"
            history.append({"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": call[0], "input": call[1]}]})
            history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": call[2]}]})
        history.append({"role": "assistant", "content": reply or "(сделано)"})
        llm.trim(history)
        save_history()


MUSIC_ACTION = {"music_stop": "stop", "music_pause": "pause", "music_resume": "resume", "music_next": "next",
                "music_prev": "previous", "volume_up": "louder", "volume_down": "quieter", "what_playing": "status"}


def _local_call(intent, r, result, reply):
    """A router command as the tool call Claude would have made: (tool, input, result) or None."""
    done = "[выполнено сразу по команде, без тебя] "
    if intent == "print_screen":
        return "print_screen", {}, done + (result or reply or "")
    if r.get("_call"):
        return r["_call"][0], r["_call"][1], done + (result or reply or "Сделано.")
    if intent == "music_play" and r.get("query"):
        return "music_play", {"query": r["query"], "kind": r.get("kind") or "track"}, done + (reply or result or "Включила.")
    if intent in MUSIC_ACTION:
        return "music_control", {"action": MUSIC_ACTION[intent]}, done + (reply or music.PLAYER.describe() or "Сделано.")
    if intent.startswith("screen_"):
        inp = {"action": intent[7:]} | ({"n": int(r["n"])} if r.get("n") is not None else {})
        return "screen", inp, done + screen.describe()
    if intent == "avatar":
        return "avatar", {"mode": r.get("mode") or "ascii"}, done + (reply or "Сделано.")
    if intent == "panel" and r.get("panel") in ("chat", "stats"):
        return "panel", {"panel": r["panel"], "show": bool(r.get("show", True))}, done + "Сделано."
    return None


def show(presentation):
    """A tool's presentation goes on the TV."""
    screen.present(presentation)


def interrupted(gen, said):
    """She was stopped: history keeps only what she really said, so she knows
    she was cut off (she once said "I got to the end" after a "стоп")."""
    note = (" ".join(said) + " [тебя перебили на этом месте]") if said else \
        "[тебя перебили, ты не успела ничего сказать]"
    entry = REPLIES.pop(gen, None)
    if entry is not None:
        entry["content"] = note
    elif history and history[-1]["role"] == "user":
        history.append({"role": "assistant", "content": note})
    save_history()


VOICE.on_stopped = interrupted


# ------------------------------------------------------------ interruptions --
def echo_fragment(w, own):
    """One of her own words, or a broken piece of one ("тилось" from
    "засветилось") cut by a recognition window."""
    if w in own:
        return True
    return len(w) >= 3 and any(o != w and (o.endswith(w) or o.startswith(w)) for o in own)


def start_listening(first):
    with LISTEN_LOCK:
        LISTEN.update(active=True, parts=[first] if first else [], deadline=time.time() + LISTEN_IDLE,
                      owner=OWNER["person"], voice=None)
    ui({"type": "think"})
    threading.Thread(target=_listen_watch, daemon=True).start()


def _listen_watch():
    while True:
        time.sleep(0.2)
        with LISTEN_LOCK:
            if not LISTEN["active"]:
                return
            if time.time() >= LISTEN["deadline"]:
                _listen_finish("nothing more said")
                return


def _listen_finish(why):
    """Under LISTEN_LOCK: what interrupted her, as one utterance - and who it was.
    Someone else than the person she was answering: she asks them to wait,
    finishes the first answer, then turns to them."""
    text = " ".join(p for p in LISTEN["parts"] if p).strip()
    owner, voice = LISTEN.get("owner") or identity.GUEST, LISTEN.get("voice")
    LISTEN.update(active=False, parts=[], voice=None)
    person = identity.GUEST
    if voice:
        label, _, known = who_said({"voice": voice[0], "dur": voice[1], "speaker": voice[2]})
        person = identity.from_voice(label, known)
    other = person.known and owner.known and owner.profile != person.profile
    words = text.split()
    content = [w for w in words if not STOP.fullmatch(w) and not LOOSE_NAME.fullmatch(w)]
    said_something = content or any(LOOSE_NAME.fullmatch(w) for w in words)
    if other:
        log(f"interrupted by {person.name} while answering {owner.name} ({why}): {text!r}")
        if said_something:
            WORK.put((text, person, owner))
        else:
            WORK.put((CONTINUE, owner, None))
        return
    if said_something and music_command(re.findall(r"\w+", text.lower()), {}, addressed=True):
        return
    if said_something:
        log(f"after interruption ({why}): {text!r}")
        WORK.put((text, person, None))
    else:
        log(f"interruption was a bare stop: {text!r}")
        ui({"type": "stop"})


# ----------------------------------------------------------------- phrases --
LATENCY = {"end": None}


def _first_sound(gen, t):
    """The reaction time: from the end of the last phrase heard to her first sound
    (both on the Station's clock: the ear's phrase end, the body's playback)."""
    end = LATENCY["end"]
    if end and 0 < t - end < 60:
        log(f"latency: phrase end -> first sound {t - end:.2f}s")


def on_phrase(item):
    text = item.get("text", "").strip()
    if len(text) < 2:
        return
    snd = item.get("sound") or {}
    if snd.get("verdict") == "noise" and not NAME.search(text):
        # not a voice (PANNs: a bird, a car, music, a door) - the STT made words of it
        # (todo 1, 2026-11-26: «это» from the canaries); her name in it still counts
        log(f"noise ({', '.join(f'{l} {p}' for l, p in snd.get('top', [])[:2])}, voice {snd.get('voice')}): {text!r}")
        hub_ear.mark_echo(item, flag="noise")
        ui({"type": "hear", "state": "ignored", "text": text})
        return
    if item.get("muted") and not (NAME.search(text) and (item.get("speaker") or {}).get("known")):
        # from a muted place (the «Места» page: "mute" - the TV; or outside the "only" places):
        # no listeners, no router, nothing to learn (a Polish cartoon, 2026-11-26). Her name
        # said by a family voice still counts - nobody can lock themselves out.
        m = item["muted"]
        log(f"muted ({m.get('place') or 'outside the listened places'}, {m.get('why')}): {text!r}")
        ui({"type": "hear", "state": "ignored", "text": text})
        return
    if _to_listeners(item):
        # a music listener took the phrase (its words, no model question): it plays the track itself.
        # The dialog also sent «рошом включи сборку» to Claude: $0.18 for a silence (2026-11-27 17:51)
        log(f"taken by a music listener, not to the dialog: {text!r}")
        ui({"type": "hear", "state": "called", "text": text})
        _remember_command(text, item)
        return
    if item.get("end") and not item.get("window"):
        LATENCY["end"] = item["end"]
    VOICE.on_first = _first_sound
    raw = item.get("raw_text") or ""
    # «евас» passed for her name, so the raw «ева стоп» was not taken and the music only ducked
    # (2026-11-27 14:11:58): the raw words win when only they hold the name, or a music command too
    if raw and music.PLAYER.active() and _named_or_stop(raw) and (
            not _named_or_stop(text) or (_music_cmd(raw) and not _music_cmd(text))):
        # the ear's canceller bit the words over the music, the raw mics did not (hear.py):
        # «евосток» there, «ева стоп» here (2026-11-27)
        log(f"over music: {text!r} -> the raw mics' {raw!r}")
        text = raw
    words = re.findall(r"\w+", text.lower())
    with LISTEN_LOCK:
        if LISTEN["active"]:
            # she stopped for him: this is his; only the tail of her own last
            # words at the very start is dropped
            own = VOICE.own_words()
            while words and echo_fragment(words[0], own):
                words.pop(0)
            LISTEN["parts"].append(" ".join(words))
            if item.get("voice"):
                LISTEN["voice"] = (item["voice"], item.get("dur", 0), item.get("speaker"))
            if item.get("window"):
                LISTEN["deadline"] = time.time() + LISTEN_IDLE
            else:
                _listen_finish("his phrase ended")
            return
    if music_command(words, item):
        _remember_command(text, item)
        return
    named = bool(NAME.search(text))
    maybe_named = named or bool(MAYBE_NAME.search(text))
    if maybe_named and re.search(r"\b(еву|евы|евой|еве)\b", text, re.I) and not named:
        maybe_named = False            # her name in an oblique case only: talk about her
    t_speech = item.get("speech_start") or item.get("start") or time.time()
    if VOICE.speaking() or VOICE.heard_while_talking(t_speech, item.get("end", time.time())) \
            or item.get("playback", 0) > 0.3:
        # said while she was talking: only a stop or her name gets through, the
        # rest is her own echo (recognised late). A stop word or her name that
        # she did not say herself wins over "mostly her words": her echo drowns
        # the speaker, so his "стоп" comes inside a phrase of hers.
        if item.get("self_sim", 0) >= SELF_SIM and item["self_sim"] > item.get("fam_sim", 0) + SELF_MARGIN:
            log(f"own voice ({item['self_sim']} / family {item.get('fam_sim')}): {text!r}")
            hub_ear.mark_echo(item)
            return
        own = VOICE.own_words()
        foreign = [w for w in words if not echo_fragment(w, own)]
        now = time.time()
        if foreign:
            HEARD.append((now, " ".join(foreign)))
        while HEARD and now - HEARD[0][0] > 6:
            HEARD.pop(0)
        hit = [w for w in foreign if (STOP.fullmatch(w) and (w not in WEAK_STOP or len(foreign) <= 2))
               or LOOSE_NAME.fullmatch(w)]
        if hit:
            log(f"interrupted by: {text!r} ({' '.join(hit)})")
            VOICE.stop()
            said = " ".join(t for _, t in HEARD)
            HEARD.clear()
            start_listening(said)
            if not item.get("window"):
                with LISTEN_LOCK:
                    if LISTEN["active"]:
                        _listen_finish("phrase ended with the interruption")
            return
        # a short answer said over the tail of her own words («отлично» right
        # after "передай…"): her words are dropped, the rest is the answer
        if foreign and not item.get("window") and len(foreign) * 2 >= len(words) \
                and relay_take(" ".join(foreign), item):
            return
        if VOICE.is_echo(text):
            log(f"own echo: {text!r}")
            hub_ear.mark_echo(item)
        return
    # an answer awaited for someone in Telegram (after the echo filter above)
    if relay_take(text, item):
        return
    # who is it
    label, sim, known = who_said(item)
    person = identity.from_voice(label, known)
    near = (item.get("speaker") or {}).get("nearest") if not known else None
    if near and near.get("sim", 0) >= GUESS_SIM and near["label"] in VOICES.names():
        # a short phrase the voice base left unsigned, but plainly a family voice («ева ты тут»
        # at 0.68 went unanswered, 2026-11-26): she answers it as that person, guessed - no
        # private notes, no acting in the house on a guess
        person = identity.from_voice(near["label"], True)
        person.guessed = True
        log(f"voice: {label} ({sim}) - taken for {person.name} by the nearest voice {near['sim']}")
    else:
        log(f"voice: {label} ({sim})")
    if introduction(text, item, label, sim, known, person):
        return
    if CALL_ONLY.match(" ".join(words)):
        # just her name: she was called, the question follows
        state["called_until"] = time.time() + CALL_WINDOW
        log(f"called: {text!r}")
        ui({"type": "hear", "state": "called", "text": text})
        return
    if STOP.search(text) and len(words) <= 3:
        return                                     # "стоп" when she is quiet: nothing to do
    # the window counts from when he BEGAN to speak (what the attention bar
    # showed him), not from when the phrase came back recognised: answers begun
    # with the bar still running were "not for Eva" (2026-11-19 evening)
    began = item.get("speech_start") or time.time()
    in_dialog = began - dialog_end() < DIALOG_WINDOW
    # every phrase by its own voice: the dialog window is for the one she talks
    # with, not for whoever speaks next ("теперь можешь разговаривать" - Иван to
    # Лиза, unrecognised - she took as Лиза's and for her, 2026-11-19)
    other = not same_speaker(person, item.get("voice"), item.get("doa"))
    if in_dialog and other:
        log(f"dialog window: another voice ({label}), not taken as the conversation")
        in_dialog = False
    # the same person goes on right after asking, before she has said a word:
    # «Ева, напиши моей маме» + pause + «пусть ответит, когда приедет» - the
    # second half was dropped as side talk (2026-11-23)
    with PENDING_LOCK:
        waiting = PENDING["text"] is not None
    if waiting and not other:
        # the rest of a request she is still waiting with: one request
        log(f"the rest of the request: {text!r}")
        pend(text, person, None, open_end=False)
        return
    following = not other and began - state.get("asked_at", 0.0) < FOLLOW_WINDOW
    unanswered = following and state.get("asked_at", 0.0) > state["last_spoke"]
    in_dialog = in_dialog or following
    called = began < state["called_until"]
    state["called_until"] = 0.0
    if music.PLAYER.active() and not (maybe_named or called or in_dialog):
        # over music only her name breaks in (Иван, 2026-11-23): no router, no
        # Claude, no ducking for anything said without it
        log(f"music plays, no name: {text!r}")
        ui({"type": "hear", "state": "ignored", "text": text})
        return
    # the local router first (shn-linux GPU): not for her / a simple command
    # done here / Claude. Without it (down): the old gate.
    last = history[-1]["content"] if history and history[-1]["role"] == "assistant" else None
    # the media library's tiles on the TV (a minute since the last step): «открой один», «назад» are
    # meant for the screen, whoever says them - the tiles open silently, so no answer opened the
    # conversation window and «открой один» was "not for Eva" (2026-11-27 20:54). Only the router's
    # local commands use this: an "ignore" here is dropped, never kept for Claude.
    p0 = screen.state.get("p") or {}
    screen_ctx = p0.get("kind") == "library" and time.time() - screen.state.get("touched", 0) < 60
    r = router.route(text, named=maybe_named or called, in_dialog=(in_dialog and not other) or screen_ctx,
                     music_on=music.PLAYER.active(), screen=screen.describe() if screen.state["p"] else None,
                     last_reply=last if isinstance(last, str) else None)
    if r is None:
        addressed = maybe_named or in_dialog or called or gate(text)
    else:
        log(f"router {r['ms']}ms: {r['intent']} {({k: v for k, v in r.items() if k not in ('intent', 'ms')}) or ''}")
        if screen_ctx and not (maybe_named or called or (in_dialog and not other)) \
                and not r["intent"].startswith(("screen_", "library_", "video_")):
            r["intent"] = "ignore"         # tiles up, no name, no talk with her: only the screen's commands
        if r["intent"] == "ignore" and (maybe_named or called):
            r["intent"] = "claude"         # called by name: never dropped
        addressed = r["intent"] != "ignore"
        if in_dialog and r["intent"] in ("ignore", "unclear"):
            # in a conversation with this same person nothing is dropped: Claude
            # has the context and answers <молчу> if it was not for her. The answer
            # to her own question «Что написать маме?» came as "unclear" and was
            # thrown away; 7 of 14 phrases dropped after her replies were real
            # follow-ups (2026-11-19..23)
            log(f"kept in the conversation ({r['intent']} by the router)")
            r["intent"] = "claude"
            addressed = True
        # garbage said to her by name: "не расслышала" (free); without her name
        # and outside a conversation it is mostly a remark in between - silence
        if r["intent"] == "unclear" and not (maybe_named or called):
            addressed = False
    if person.known or (addressed and r is not None and r["intent"] not in ("claude", "ignore", "unclear")):
        # long-term memory keeps the voices it knows (as the old assistant did):
        # not children's chatter of unknown guests, not the TV - but a command she carried out
        # is hers whoever said it («включи семь» in an unknown voice stood as mere talk, 2026-11-27)
        memory.write("user", text, speaker=person.profile, dialog=in_dialog, addressed=addressed,
                     voice_sim=sim, dur=item.get("dur"), audio=item.get("audio"), audio_span=item.get("audio_span"))
    if addressed and r is not None and r["intent"] != "claude":
        state.update(talker=person, talker_voice=item.get("voice"), talker_doa=item.get("doa"), asked_at=began)
        WORK.put((text, person, r))
        return
    if addressed:
        prev, note = state.get("talker"), None
        if other and prev is not None and not named:
            # she gets told it is not her last talker (an unknown voice is not "probably the same")
            note = (f"[система: это другой голос, не {prev.name or 'тот, с кем ты говорила'}; "
                    f"тебя по имени не назвали - скорее всего, люди говорят между собой]")
        elif unanswered:
            note = "[система: это продолжение предыдущей просьбы того же человека - она ещё не договорила, когда ты начала отвечать]"
        state.update(talker=person, talker_voice=item.get("voice"), talker_doa=item.get("doa"), asked_at=began)
        # not to Claude at once: the person may go on after a pause (pend)
        pend(text, person, note, open_end=r is not None and r.get("complete") is False)
    else:
        log(f"not for Eva: {text!r}")
        ui({"type": "hear", "state": "ignored", "text": text})


# A request goes to Claude only after a short wait: the phrase ended at a pause,
# but the person may not be done («Ева, напиши моей маме» - pause - «пусть
# ответит, когда приедет»: she was already thinking while Лиза went on,
# 2026-11-23). While the ear hears someone (HEARING_FLAG) she keeps waiting; the
# same person's next phrase is joined to the request. The ring shows "listening"
# meanwhile, "thinking" only once the request is on its way.
GRACE = profile.tune("dialog.grace", 0.8)              # s after a finished-sounding request
GRACE_OPEN = profile.tune("dialog.grace_open", 2.5)    # s after one the router calls unfinished
GRACE_MAX = profile.tune("dialog.grace_max", 12.0)     # s at most, however long others talk
HEARING_FLAG = "/run/eva/hearing"                      # ear.py: a phrase is being heard
PENDING = {"text": None, "person": None, "note": None, "until": 0.0, "since": 0.0}
PENDING_LOCK = threading.Lock()


def pend(text, person, note, open_end):
    with PENDING_LOCK:
        if PENDING["text"] is None:
            PENDING.update(text=text, person=person, note=note, since=time.time())
            threading.Thread(target=_pending_watch, daemon=True).start()
        else:
            PENDING["text"] += " " + text
        PENDING["until"] = time.time() + (GRACE_OPEN if open_end else GRACE)
        shown = PENDING["text"]
    ui({"type": "hear", "state": "held", "text": shown})


def _pending_watch():
    while True:
        time.sleep(0.1)
        with PENDING_LOCK:
            now = time.time()
            heard = (hub_ear.hearing() if hub_ear.STATE["active"] else os.path.exists(HEARING_FLAG))                 and now - PENDING["since"] < GRACE_MAX
            if now < PENDING["until"] or heard:
                continue
            text, person, note = PENDING["text"], PENDING["person"], PENDING["note"]
            PENDING["text"] = None
        log(f"request: {text!r}")
        ui({"type": "think", "text": text})
        WORK.put((text, person, None, note))
        return


SAME_VOICE = profile.tune("dialog.same_voice", 0.5)             # CAM++ similarity of two phrases of one person


# Direction (ear -> stt_server /doa: delays of 21 mic pairs). Measured 2026-11-23
# on 5 places in the room: phrases from one place differ by 0.06-0.39 (mean abs,
# samples), different places by 0.37-0.61; a phrase finds its own place 66/71.
# So it tells places apart, not two people standing on the same side.
DIR_SAME = profile.tune("dialog.direction_same", 0.25)
DIR_OTHER = profile.tune("dialog.direction_other", 0.45)


def direction_gap(doa, prev_doa):
    """Mean difference of two direction fingerprints, or None."""
    if not doa or not prev_doa or len(doa.get("lags", [])) != len(prev_doa.get("lags", [])):
        return None
    return float(np.mean(np.abs(np.asarray(doa["lags"]) - np.asarray(prev_doa["lags"]))))


def same_speaker(person, voice, doa=None):
    """Is this phrase by the person she talked with last? Known voices by
    their name; otherwise by the voice itself, and where the voice cannot tell,
    by the direction it came from."""
    prev, pv = state.get("talker"), state.get("talker_voice")
    if prev is None:
        return True
    if person.known and prev.known:
        return person.name == prev.name
    gap = direction_gap(doa, state.get("talker_doa"))
    if voice is None or pv is None:
        # no voice to tell by: the direction, else the benefit of the doubt
        return gap is None or gap < DIR_OTHER
    # one of them unrecognised: by the voice itself. Recognition flickers in a
    # new room (Иван: "Иван", then a guest, then "Иван") and his answers in the
    # window were ignored as "another voice" (2026-11-19 evening); two phrases of
    # one person in one room are far closer to each other than to his profile
    a, b = np.asarray(voice, dtype=np.float32), np.asarray(pv, dtype=np.float32)
    sim = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    if gap is not None and abs(sim - SAME_VOICE) < 0.1:
        # the voice is on the fence: the place decides
        if gap <= DIR_SAME:
            return True
        if gap >= DIR_OTHER:
            return False
    return sim >= SAME_VOICE


# while music plays: a short command without her name (as in the old assistant)
MUSIC_WORDS = [(("стоп", "хватит", "выключи", "выключай"), "stop"), (("пауза", "паузу"), "pause"),
               (("дальше", "следующая", "следующую", "следующий", "пропусти", "другую"), "next"),
               (("назад", "предыдущая", "предыдущую"), "previous"), (("тише", "потише"), "quieter"),
               (("громче", "погромче"), "louder"), (("продолжи", "продолжай", "играй"), "resume")]


# A message passed aloud at home for someone in Telegram: what is said at home
# next is the answer, sent back to them by the system - Иван's «отлично» to
# Маша's message stayed with Eva, unrecognised and taken as not for her
# (2026-11-20).
RELAY_WAIT = profile.tune("dialog.relay_wait", 180)              # s she waits for the answer
RELAY_GAP = profile.tune("dialog.relay_gap", 4.0)               # s of quiet after the last phrase: the answer is complete
RELAY = {"to": None, "until": 0.0, "parts": [], "timer": None}
RELAY_LOCK = threading.Lock()


def relay_start(person):
    with RELAY_LOCK:
        RELAY.update(to=person, until=time.time() + RELAY_WAIT, parts=[])


def relay_take(text, item):
    """A phrase at home while an answer is awaited: collected for the sender."""
    with RELAY_LOCK:
        if RELAY["to"] is None or time.time() > RELAY["until"]:
            RELAY["to"] = None
            return False
        label, sim, known = who_said(item)
        who = identity.from_voice(label, known).name if known else None
        RELAY["parts"].append((who, text))
        if RELAY["timer"]:
            RELAY["timer"].cancel()
        RELAY["timer"] = threading.Timer(RELAY_GAP, relay_send)
        RELAY["timer"].start()
    log(f"relay: {text!r} ({who or 'voice not recognised'})")
    ui({"type": "hear", "state": "called", "text": text})
    return True


def relay_send():
    from skills.home import notify
    with RELAY_LOCK:
        to, parts = RELAY["to"], RELAY["parts"]
        RELAY.update(to=None, parts=[], timer=None)
    if not to or not parts:
        return
    names = {w for w, _ in parts if w}
    who = ", ".join(sorted(names)) if names else "голос не узнан"
    text = f"Ответ дома ({who}): «{' '.join(t for _, t in parts)}»"
    ok = notify(to, text)
    log(f"relay to {to.name}: {text!r} - {'sent' if ok else 'NOT sent'}")
    VOICE.say("Передала ответ." if ok else "Не получилось передать ответ.")
    history.append({"role": "assistant", "content": f"[переслала {to.name} в Телеграм] {text}"})
    save_history()


MUSIC_CMD_ONCE = profile.tune("dialog.music_cmd_once", 2.5)          # s: the same music command again is the same command
MUSIC_FILLER = {"пожалуйста", "музыку", "музыка", "песню", "песня", "трек", "эту", "это", "ну", "а", "давай",
                "включи", "сделай", "немного", "чуть", "чуть-чуть", "по", "поставь", "на"}


def music_down():
    """The music goes down only when she is wanted: her name was heard (the
    call window), she listens after being interrupted, works on an answer or
    talks. Any voice over the music turned it down before - nothing could be
    listened to in a talking room (Иван, 2026-11-21)."""
    return (VOICE.speaking() or LISTEN["active"] or state.get("answering")
            or time.time() < state["called_until"])


def _remember_command(text, item):
    """A phrase done as a command before the dialog's own bookkeeping (a music command over the music,
    a listener's words): into the memory as said to her - the journal showed «ева стоп» and «включи
    сборку» as mere heard talk, the router's commands as said to Eva (2026-11-27)."""
    try:
        label, sim, known = who_said(item)
        near = (item.get("speaker") or {}).get("nearest") if not known else None
        if near and near.get("sim", 0) >= GUESS_SIM and near["label"] in VOICES.names():
            label, known = near["label"], True
        person = identity.from_voice(label, known)
        memory.write("user", text, speaker=person.profile if person.known else None, dialog=False, addressed=True,
                     voice_sim=sim, dur=item.get("dur"), audio=item.get("audio"), audio_span=item.get("audio_span"))
    except Exception as e:
        log(f"command not remembered: {e}")


def music_command(words, item, addressed=False):
    """«Ева, стоп / пауза / дальше / тише…» while music plays (or is paused),
    up to three words besides her name, not over her voice. Only with her name
    - in the phrase, or just called («Ева…» pause «тише»), or `addressed` (she
    was interrupted by name): over music nothing else may break in (Иван,
    2026-11-23: "интерраптить её должно только имя"). True if it was one."""
    if not music.PLAYER.active() or VOICE.speaking():
        return False
    # «Ева, стоп» fused by the recogniser into one word over the music: «евосток» - the
    # music could not be stopped (2026-11-27). One word only: «и восток» is two.
    if len(words) == 1 and FUSED_STOP.match(words[0]):
        log(f"music: {words[0]!r} taken for «Ева, стоп»")
        words = ["ева", "стоп"]
    # her name, "please", "музыку" and a repeated word are not the command:
    # «ева выключи выключи музыку» went to Claude for $0.16 (2026-11-21)
    named = any(NAME.fullmatch(w) for w in words)
    core = []
    for w in words:
        if NAME.fullmatch(w) or w in MUSIC_FILLER or (core and core[-1] == w):
            continue
        core.append(w)
    if not 1 <= len(core) <= 3:
        return False
    if not (named or addressed or time.time() < state["called_until"]):
        return False
    words = core
    for keys, action in MUSIC_WORDS:
        if any(w in keys for w in words):
            p = music.PLAYER
            # one command, heard twice: by a window over the music and by the
            # phrase's tail («включи следующий трек» + «следующий трек») - it
            # skipped two tracks (2026-11-20)
            if state.get("music_cmd") == action and time.time() - state.get("music_cmd_at", 0) < MUSIC_CMD_ONCE:
                log(f"music command again: {' '.join(words)!r} -> {action} (ignored)")
                return True
            state.update(music_cmd=action, music_cmd_at=time.time())
            state["called_until"] = 0.0        # «Ева… дальше»: done, the music comes back up
            ui({"type": "hear", "state": "called", "text": " ".join(words)})   # the ring: she took it
            if action == "stop":
                p.stop()
            elif action in ("pause", "resume"):
                p.pause(action == "pause")
            elif action in ("next", "previous"):
                p.next(1 if action == "next" else -1)
            else:
                p.louder(action == "louder")
            log(f"music command: {' '.join(words)!r} -> {action}")
            ui({"type": "hear", "state": "called", "text": " ".join(words)})
            return True
    return False


def attention_until():
    """When her attention ends: a phrase before that is taken as meant for her
    without her name. -> (until, total) in seconds, or (0, 0)."""
    if VOICE.speaking():
        return time.time() + DIALOG_WINDOW, DIALOG_WINDOW
    talk = dialog_end() + DIALOG_WINDOW
    until, total = max((talk, DIALOG_WINDOW), (state["called_until"], CALL_WINDOW))
    return (until, total) if until > time.time() else (0.0, 0.0)


def attention_watch():
    """The attention bar on the screen and the LED ring: sent when it changes."""
    last = None
    while True:
        until, total = attention_until()
        cur = (round(until, 1), total, VOICE.speaking())
        if last is None or abs(cur[0] - last[0]) > 0.3 or cur[1:] != last[1:]:
            ui({"type": "attention", "until": int(until * 1000), "total": int(total * 1000),
                "speaking": cur[2]})
            last = cur
        time.sleep(0.25)


def _to_listeners(item):
    """Every logged phrase to the listeners (core/listeners.py) - not her own voice."""
    if item.get("self_sim", 0) >= SELF_SIM and item["self_sim"] > item.get("fam_sim", 0) + SELF_MARGIN:
        return
    try:
        label, _, known = who_said(item)
    except Exception:
        label, known = "unknown", False
    near = (item.get("speaker") or {}).get("nearest") if not known else None
    if near and near.get("sim", 0) >= GUESS_SIM and near["label"] in VOICES.names():
        # the same guess the dialog makes (on_phrase): a short phrase left unsigned but plainly
        # a family voice is that person for the listeners too - «немечка хуя» from Лиза
        # (0.67) passed as "an unknown adult" and got «не ругайтесь при детях» (2026-11-26)
        label, known = near["label"], True
    if not known and music.PLAYER.active():
        # her own music is heard too, and a record's voice is never a family one: a listener
        # that plays «давайте жить дружно» on those words would start itself again from the
        # song (2026-11-27). Over her music only family voices reach the listeners.
        return
    name = voices.CALL.get(label, label) if known else None
    if near:
        near = dict(near, name=voices.CALL.get(near["label"], near["label"]))
        near["child"] = near["name"] in profile.CHILDREN or near["label"] in profile.CHILDREN
    child = bool(name and (name in profile.CHILDREN or label in profile.CHILDREN))
    # what the sound says (PANNs): a child's voice, whoever the voice base thinks it is
    top = (item.get("sound") or {}).get("top") or []
    child = child or any(l.startswith("Child") and p >= 0.3 for l, p in top)
    listeners.feed(item, name, known, child, label if known else None, near)
    return listeners.claims(item.get("text", ""), name, known, child, label if known else None, near)


def who_said(item):
    """(label, similarity, known): decided on the hub with the phrase (its voice
    base, services/stt/voices.py) - asked again only for an item without it."""
    sp = item.get("speaker")
    if sp:
        return sp["label"], sp["sim"], sp["known"]
    if item.get("voice"):
        return VOICES.identify(item["voice"], item.get("dur", 0))
    return "unknown", 0.0, False


def introduction(text, item, label, sim, known, person):
    """"знакомство": learning a voice. True if the phrase was handled here."""
    if VOICES.enrolling():
        n = VOICES.enroll_count()
        if n >= 3:
            name, cnt, moved = VOICES.finish_enroll()
            log(f"enrolled {name}: {cnt} samples, merged {moved}")
            VOICE.say(f"Готово, я запомнила голос: {voices.CALL.get(name, name)}.")
        else:
            VOICE.say("Ещё одну фразу." if n else "Слушаю, скажи ещё что-нибудь.")
        return True
    m = INTRO.search(text) or ENROLL.search(text)
    name = intro_name(text, m) if m else None
    if not name or "голос" not in text.lower():
        return False
    if known:
        # a voice she already knows: no new profile under another name. Refused
        # only when it is the name of somebody else in the family; a garbled
        # name is the speaker's own ("я Лиза" came as "Аит", 2026-11-19)
        other = next((p for p in VOICES.names() if p != label and voices.same_person(p, name)), None) \
            or next((n for s, n in FAMILY if name.lower().startswith(s) and not voices.same_person(label, n)), None)
        # close voices (sisters: 0.62-0.67 to each other) pass for each other, so
        # «я Соня» from a voice taken for Лиза is refused only when it is
        # clearly Лиза's (2026-11-24)
        mine = None
        if other and not voices.same_person(label, name) and item.get("voice"):
            if other in VOICES.names():
                mine = VOICES.claim(item["voice"], other, lead=-voices.ID_MARGIN)[0]
            elif sim < voices.ENROLL_KNOWN_MAX:
                mine = other            # a family member with no voice yet
        if voices.same_person(label, name) or not other:
            # asked by the owner of the profile: refresh it with samples from today's microphones
            VOICES.start_enroll(label, first=item.get("voice"))
            log(f"refreshing voice {label} ({sim})")
            VOICE.say(f"Узнаю тебя, {person.name}. Скажи ещё пару фраз, я обновлю твой голос.")
        elif mine:
            VOICES.start_enroll(mine, first=item.get("voice"))
            log(f"voice {label} ({sim}) says it is {name}: close voices, enrolling {mine}")
            VOICE.say(f"Ой, вы с {voices.CALL.get(label, label)} звучите похоже. Привет, {voices.CALL.get(mine, mine)}! "
                      f"Скажи ещё пару фраз, я запомню твой голос получше.")
        else:
            VOICE.say(f"Не-а, я тебя уже знаю: ты {person.name}!")
            log(f"introduction refused: {label} ({sim}) said to be {name}")
        return True
    profile, csim = VOICES.claim(item["voice"], name) if item.get("voice") else (None, 0.0)
    if not profile and item.get("voice"):
        # not recognised, and not clearly somebody else either (a sister's voice is as close)
        profile, csim = VOICES.claim(item["voice"], name, lead=-voices.ID_MARGIN)
    if profile:
        # the owner in other acoustics (Иван, another room: 0.44-0.49 to his
        # profile, 0.25 to the others) - refresh, not "impostor" (2026-11-19)
        VOICES.start_enroll(profile, first=item.get("voice"))
        log(f"refreshing voice {profile} from an unrecognised voice ({csim}) that says it is {name}")
        VOICE.say(f"Похоже, это правда ты, {voices.CALL.get(profile, profile)}, просто звучишь иначе. "
                  f"Скажи ещё пару фраз, я обновлю твой голос.")
        return True
    if name.lower() in {n.lower() for n in VOICES.names()} or any(voices.same_person(p, name) for p in VOICES.names()):
        VOICE.say(f"Я уже знаю голос по имени {name}, и это не твой. Как тебя зовут на самом деле?")
        log(f"introduction refused: unknown voice ({sim}) claims existing profile {name}")
        return True
    VOICES.start_enroll(name, first=item.get("voice"))
    log(f"enrolling voice {name}")
    VOICE.say(f"Привет, {name}! Скажи мне ещё пару фраз, любых, я запоминаю твой голос.")
    return True


# An unrecognised voice never reaches Claude (user, 2026-11-26): at home it is mostly a
# family member's short phrase the voice base left unsigned, or the canaries, a phone,
# the TV - and each such request cost a cache of its own. The router's commands (music,
# volume, screen, print) and «запомни мой голос» stay for every voice: they are free.
UNKNOWN_TO_CLAUDE = profile.tune("dialog.unknown_to_claude", False)
GUESS_SIM = profile.tune("dialog.guess_sim", 0.55)     # an unsigned phrase this close to a family voice is that person
UNKNOWN_SAY = profile.tune("dialog.unknown_say_every", 120.0)   # s between two «я тебя не узнала»
_unknown_said = [0.0]


def dialog_end():
    """When her last answer to somebody ended: the conversation window runs from there.
    Only an answer opens it - her own words (the boot greeting, a reminder, a listener's
    «не ругайтесь при детях», «сказать дома») do not: after the greeting the room's talk was
    taken for a conversation with her and she asked «что ты сказал?» (2026-11-26)."""
    la = state.get("last_answer", 0.0)
    return max(la, VOICE.busy_until) if time.time() - la < 120 else la


def unknown_voice(text):
    log(f"unknown voice - not to Claude: {text!r}")
    ui({"type": "hear", "state": "ignored", "text": text})
    if NAME.search(text) and time.time() - _unknown_said[0] > UNKNOWN_SAY:
        _unknown_said[0] = time.time()
        VOICE.say("Я не узнала твой голос. Скажи: «Ева, запомни мой голос» и своё имя.")


def worker():
    while True:
        text, person, owner, *note = WORK.get()
        state["answering"] = True          # the music stays down while she works on it
        try:
            if isinstance(owner, dict):    # a command from the router's catalog: no Claude
                local(text, person, owner)
                continue
            if not person.known and not UNKNOWN_TO_CLAUDE and text != CONTINUE:
                unknown_voice(text)
                continue
            answer(text, person, owner, note[0] if note else None)
        except Exception as e:
            log("answer failed:", e)
        finally:
            state["answering"] = False
