"""Skill "дом": who was heard at home and when, saying something aloud at home,
writing to a family member in Telegram. With these she can act on «Ева, где
Маша? она не берёт трубку» from Telegram: look who was heard lately, call Маша
aloud at home, pass the answer back (docs/ARCHITECTURE.md, agent)."""
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

import memory
import photolib
import voices
from core.skills import Skill, Tool
from core.util import log, secrets


SURE_SEC, SURE_SIM = 2.0, 0.7    # a phrase this long, or a match this strong, is that person
NOW_MIN = 10                     # heard within this many minutes = probably still here


def _profile(name):
    """A family name as said («Маша», «Иван», «папа») -> its voice profile label, or None."""
    for p in voices._load(voices.PROFILES):
        if voices.same_person(p, name) or name.lower().startswith(p.lower()[:4]):
            return p
    return None


def _ago(ts):
    m = int((time.time() - ts) // 60)
    if m < 1:
        return "только что"
    if m < 60:
        return f"{m} мин назад"
    return f"{m // 60} ч {m % 60} мин назад" if m < 24 * 60 else f"{m // 1440} дн назад"


def heard(args, person):
    """The last phrases of a family member the Station heard (the transcript
    keeps the voices it knows), newest last."""
    name = args["name"]
    prof = _profile(name)
    if not prof:
        return f"Голоса «{name}» я не знаю, поэтому не могу сказать, когда слышала."
    hours = float(args.get("hours") or 24)
    since = time.time() - hours * 3600
    rows = []
    for day in sorted(os.listdir(memory.DIR))[-3:] if os.path.isdir(memory.DIR) else []:
        try:
            lines = open(os.path.join(memory.DIR, day), encoding="utf-8", errors="ignore").read().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.startswith("{"):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("role") != "user" or r.get("speaker") != prof or r.get("stt") == "telegram":
                continue
            ts = datetime.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
            if ts >= since:
                # sure: a longer phrase or a strong match; one short word may be someone else
                sure = (r.get("dur") or 0) >= SURE_SEC or (r.get("voice_sim") or 0) >= SURE_SIM
                rows.append((ts, r["text"], sure))
    call = voices.CALL.get(prof, prof)
    if not rows:
        return (f"За последние {hours:g} ч я не слышала голос «{call}» дома (слышу только у колонки, "
                f"в своей комнате; узнаю не каждую фразу).")
    sure = [r for r in rows if r[2]]
    last, last_sure = rows[-1][0], (sure[-1][0] if sure else None)
    recent = [r for r in rows if time.time() - r[0] < NOW_MIN * 60]
    # what it means, so she does not say "Маша дома" after one word heard long ago
    if len([r for r in recent if r[2]]) >= 1 and len(recent) >= 2:
        verdict = f"Вывод: {call}, скорее всего, дома, рядом с колонкой (несколько фраз за последние {NOW_MIN} мин)."
    elif recent:
        verdict = (f"Вывод: была короткая фраза похожим голосом в последние {NOW_MIN} мин - это может быть и "
                   f"не {call}. Дома ли сейчас - не уверена.")
    else:
        verdict = (f"Вывод: дома ли {call} сейчас - неизвестно: последний раз слышала {_ago(last)}, с тех пор "
                   f"могла уйти. Не говори «дома» - говори, когда слышала.")
    tail = "; ".join(f"[{time.strftime('%H:%M', time.localtime(t))}{'' if ok else ', коротко, неуверенно'}] {txt}"
                     for t, txt, ok in rows[-6:])
    return (f"{call}: последний раз слышала {_ago(last)} (в {time.strftime('%H:%M', time.localtime(last))})"
            + (f", уверенно - {_ago(last_sure)}" if last_sure and last_sure != last else "")
            + (", уверенных фраз нет" if not sure else "")
            + f"; фраз за {hours:g} ч: {len(rows)}. Последние: {tail}. {verdict}")


def say_home(args, person):
    """Aloud at home through the Station - and into the voice conversation, so
    an answer at home is understood as an answer to this."""
    import dialog                       # the voice channel lives in the brain process
    text = args["text"].strip()
    dialog.VOICE.say(text)
    dialog.history.append({"role": "assistant", "content":
                           f"[сказала вслух дома по просьбе {person.name or 'кого-то'} "
                           f"({'из Телеграма' if person.channel != 'voice' else 'голосом'})] {text}"})
    dialog.state["last_spoke"] = time.time()
    dialog.state["talker"] = None       # whoever answers at home is the one she waits for
    dialog.save_history()
    log(f"said at home for {person.name}: {text!r}")
    if person.channel != "voice":
        # she waits for the answer at home and passes it back herself
        dialog.relay_start(person)
        return (f"Сказала вслух дома. Что ответят у колонки в ближайшие {dialog.RELAY_WAIT // 60} мин, "
                f"я перешлю {person.name} в Телеграм сама (это делает система).")
    return "Сказала вслух дома."


def notify(person, text):
    """A Telegram message to this Person (their own chat with Eva), also kept in
    that chat's history. -> True if sent."""
    import chat
    u = next((u for u in photolib.users() if u.get("telegram_id") and u.get("person_id") == person.person_id), None)
    if not u:
        return False
    data = urllib.parse.urlencode({"chat_id": u["telegram_id"], "text": text}).encode()
    api = f"https://api.telegram.org/bot{secrets()['TELEGRAM_BOT_TOKEN']}/sendMessage"
    try:
        with urllib.request.urlopen(api, data=data, timeout=15) as r:
            ok = json.loads(r.read()).get("ok")
    except OSError as e:
        log(f"telegram notify {person.name}: {e}")
        return False
    if ok:
        with chat.LOCK:
            hist = chat.CHATS.setdefault(f"tg:{u['telegram_id']}", [])
            hist.append({"role": "user", "content": "[система: ниже - ответ из дома, который ты переслала сама]"})
            hist.append({"role": "assistant", "content": text})
        chat.save()
    return bool(ok)


def _family_chat(name):
    for u in photolib.users():
        pn = u.get("person_name") or ""
        if u.get("telegram_id") and (pn.lower() == name.lower() or voices.same_person(pn, name)
                                     or name.lower()[:4] == pn.lower()[:4]):
            return u
    return None


def send(args, person):
    """A Telegram message from Eva to a family member - and into that person's
    chat with her, so a reply is understood."""
    import chat
    u = _family_chat(args["name"])
    if not u:
        return f"У «{args['name']}» нет Телеграма, известного мне (photolib)."
    text = args["text"].strip()
    data = urllib.parse.urlencode({"chat_id": u["telegram_id"], "text": text}).encode()
    api = f"https://api.telegram.org/bot{secrets()['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with urllib.request.urlopen(api, data=data, timeout=15) as r:
        if not json.loads(r.read()).get("ok"):
            return "Телеграм не принял сообщение."
    with chat.LOCK:      # a reply to it is then understood as a reply to it
        hist = chat.CHATS.setdefault(f"tg:{u['telegram_id']}", [])
        hist.append({"role": "user", "content": f"[система: ниже - сообщение, которое ты сама написала "
                                                f"{u.get('person_name')} в Телеграм по просьбе {person.name}]"})
        hist.append({"role": "assistant", "content": text})
    chat.save()
    log(f"telegram to {u.get('person_name')} for {person.name}: {text!r}")
    return f"Написала {u.get('person_name')} в Телеграм."


PROMPT = """## Дом и семья - что ты можешь сделать

who_heard - когда и что последним говорил дома член семьи (слышишь только у
колонки и только узнанные голоса; короткое слово могла приписать не тому).
Слышала полчаса назад - не значит «сейчас дома»: верь его «Выводу». Если не
уверена, что человек дома, - сделай оба шага: позови вслух и напиши в Телеграм. say_at_home - сказать вслух дома через колонку
(позвать, передать). send_message - написать члену семьи в Телеграм. Камер у тебя
пока нет: увидеть, кто где, ты не можешь - только услышать."""

SKILL = Skill(
    name="home",
    summary="дом: кого и когда слышала, сказать вслух дома, написать в Телеграм",
    prompt=PROMPT,
    tools=[Tool(
        name="who_heard",
        description="Когда и что последним говорил дома член семьи (по записям того, что слышала колонка; "
                    "только узнанные голоса). Для «где Маша?», «Маша дома?», «давно Лиза пришла?».",
        schema={"type": "object", "additionalProperties": False, "required": ["name"],
                "properties": {"name": {"type": "string"},
                               "hours": {"type": "number", "description": "за сколько часов, по умолчанию 24"}}},
        handler=heard,
        needs="adult",
    ), Tool(
        name="say_at_home",
        description="Сказать вслух дома через колонку - позвать кого-то, передать сообщение («Маша, тебе "
                    "звонит Иван, перезвони ему»). Текст скажи от себя, по ситуации, коротко.",
        schema={"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"text": {"type": "string"}}},
        handler=say_home,
        needs="adult",
    ), Tool(
        name="send_message",
        description="Написать члену семьи в Телеграм от себя (у кого Телеграм есть в photolib): передать "
                    "ответ, напомнить, позвать.",
        schema={"type": "object", "additionalProperties": False, "required": ["name", "text"],
                "properties": {"name": {"type": "string"}, "text": {"type": "string"}}},
        handler=send,
        needs="adult",
    )],
)
