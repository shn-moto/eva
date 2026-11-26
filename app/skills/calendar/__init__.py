"""Skill "календарь": what she was asked to do at a time (core/calendar.py) -
remind (aloud at home and/or in Telegram), play music, wake with a smart alarm
(a gentle melody rising slowly, stopped by voice), carry out an instruction -
once or on a repeat; and the family's birthdays from the tree, said at home."""
import time

import music
from core import calendar as cal, identity, profile
from core.skills import Skill, Tool
from core.speech import volume_get, volume_set
from core.util import log

ALARM_QUERY = profile.tune("alarm.query", "calm morning piano music")   # the default melody (YouTube Music)
ALARM_START = profile.tune("alarm.start", 8)            # the player's level at the start, % (mpg123)
ALARM_PLAYER = profile.tune("alarm.player", 70)         # ... and at the end of the rise (music's usual level)
ALARM_RAMP = profile.tune("alarm.ramp", 180)            # s of rising
ALARM_MASTER = profile.tune("alarm.master", 45)         # the speakers at least this loud while it plays, %
ALARM_MAX = profile.tune("alarm.max", 1200)             # s: nobody stopped it - it stops itself
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


# ---- what an event does when its time comes ------------------------------------------------

def _person(e):
    """Who asked, as a Person (their rights): the voice profile, else the name."""
    if e.get("by_profile"):
        p = identity.from_voice(e["by_profile"], True)
        if p.known:
            return p
    return identity.Person(e.get("by"), channel="voice")


def _say(text, note):
    import dialog
    dialog.VOICE.say(text)
    dialog.history.append({"role": "assistant", "content": f"[{note}] {text}"})
    dialog.state["last_spoke"] = time.time()
    dialog.save_history()


def _telegram(e, text):
    """-> True if the message went."""
    from skills.home import send
    who = e.get("who") or e.get("by")
    try:
        out = send({"name": who, "text": text}, _person(e))
        log(f"calendar: telegram to {who}: {out}")
        return out.startswith("Написала")
    except Exception as ex:
        log(f"calendar: telegram to {who}: {ex}")
        return False


def do_remind(e):
    text = e.get("text") or e.get("title")
    who = e.get("who")
    aloud = e.get("notify") in (None, "home", "both")
    if e.get("notify") in ("telegram", "both") and not _telegram(e, f"Напоминаю: {text}"):
        aloud = True                          # Telegram did not take it: never lost silently - aloud at home
    if aloud:
        _say((f"{who}, напоминаю: " if who else "Напоминаю: ") + text, "напоминание из календаря")


def do_music(e):
    q = e.get("query") or e.get("title")
    found = music.search(q, "track", 8 if e.get("kind") == "selection" else 1)
    if found:
        music.PLAYER.play(found)
    else:
        log(f"calendar: nothing found for «{q}»")


ALARM = {"on": False}


def do_alarm(e):
    """A gentle melody, quiet at first, rising over ALARM_RAMP; «Ева, выключи»
    stops it (the music stops). The speakers' level is given back after."""
    q = e.get("query") or ALARM_QUERY
    found = music.search(q, "track", 8)
    if not found:
        log(f"calendar: alarm: nothing found for «{q}»")
        return
    p, master = music.PLAYER, volume_get()
    if master < ALARM_MASTER:
        volume_set(ALARM_MASTER, show=False)
    p.volume = ALARM_START
    p.play(found)
    ALARM["on"] = True
    t0 = time.time()
    log(f"calendar: alarm «{q}», master {master}% -> {max(master, ALARM_MASTER)}%")
    try:
        time.sleep(3)
        while time.time() - t0 < ALARM_MAX:
            if not p.active():
                break                              # stopped by voice (or the queue ended)
            k = min(1.0, (time.time() - t0) / ALARM_RAMP)
            v = int(ALARM_START + (ALARM_PLAYER - ALARM_START) * k)
            if v != p.volume and not p.paused:
                p.volume = v
                p._apply_volume()
            time.sleep(5)
        else:
            p.stop()
            log("calendar: alarm stopped itself")
    finally:
        ALARM["on"] = False
        p.volume = ALARM_PLAYER
        if volume_get() == ALARM_MASTER and master < ALARM_MASTER:
            volume_set(master, show=False)        # unless someone turned it meanwhile


def do_task(e):
    """An instruction for her at that time («в 8 расскажи погоду»): a turn of the voice
    conversation, as if asked then - she says it at home."""
    import dialog
    p = _person(e)
    # through the dialog's own queue: one answer at a time, never on top of a running one
    dialog.WORK.put((e.get("text") or e.get("title"), p, None,
                     f"[система: это задание из календаря, его поставил(а) {e.get('by') or 'кто-то'} "
                       f"{e.get('created', '')[:10]}; сейчас рядом с колонкой его может не быть - "
                       f"сделай и скажи результат вслух]"))


def say_birthdays(which, lines):
    text = (f"{'Сегодня' if which == 'сегодня' else 'Завтра'} день рождения: " + "; ".join(lines) + ".")
    if which == "сегодня":
        text += " Не забудьте поздравить!"
    _say(text, "дни рождения из семейного дерева")


cal.HANDLERS.update(remind=do_remind, music=do_music, alarm=do_alarm, task=do_task)
cal.BIRTHDAY[0] = say_birthdays


# ---- tools ---------------------------------------------------------------------------------

def _repeat_text(r):
    if not r:
        return "один раз"
    every = r.get("every")
    if every == "week":
        return "каждую неделю: " + ", ".join(DAYS[d] for d in r.get("days") or [])
    return {"day": "каждый день", "weekdays": "по будням", "weekend": "по выходным",
            "month": "каждый месяц", "year": "каждый год"}.get(every, every)


def _line(t, e):
    what = {"remind": "напомнить", "music": "музыка", "alarm": "будильник", "task": "задание"}[e["action"]]
    extra = e.get("query") or e.get("text") or ""
    return (f"{DAYS[t.weekday()][:2]} {t:%d.%m %H:%M} - {what}: {e.get('title')}"
            + (f" ({extra})" if extra and extra != e.get("title") else "")
            + (f", для {e['who']}" if e.get("who") else "")
            + f" [{_repeat_text(e.get('repeat'))}, id {e['id']}]")


def add(args, person):
    action = args["action"]
    if action == "music" and not args.get("query"):
        return "Что включить: query (исполнитель и песня)."
    if action == "task" and not args.get("text"):
        return "Что сделать: text (поручение тебе самой)."
    ev = {k: args[k] for k in ("title", "when", "repeat", "action", "text", "query", "notify", "who", "kind")
          if args.get(k) not in (None, "", {})}
    ev.update(by=person.name, by_profile=person.profile, channel=person.channel)
    if "notify" not in ev and action == "remind":
        # asked at home - said at home; from Telegram - into Telegram; for someone else - both
        ev["notify"] = "both" if args.get("who") else "home" if person.channel == "voice" else "telegram"
    try:
        e = cal.add(ev)
    except ValueError as ex:
        return f"Не добавила: {ex}"
    t = cal.parse(e["next"])
    return f"Добавила: {_line(t, e)}."


def listing(args, person):
    days = int(args.get("days") or 7)
    occ = cal.upcoming(days)
    lines = [_line(t, e) for t, e in occ[:40]]
    try:
        import photolib
        bd = photolib.birthdays(days=days)
    except Exception:
        bd = []
    out = f"Календарь на {days} дн.:\n" + ("\n".join(lines) if lines else "ничего не запланировано")
    if bd:
        out += "\nДни рождения (семейное дерево, напоминаю сама утром и накануне вечером):\n" + "\n".join(bd)
    return out


def remove(args, person):
    e = cal.remove(args["id"])
    return f"Удалила: «{e.get('title')}» ({_repeat_text(e.get('repeat'))})." if e else f"Нет события {args['id']}."


PROMPT = """## Календарь и планировщик

calendar_add - поставить в календарь: action remind - напомнить (notify: home -
вслух дома, telegram - в Телеграм, both), music - включить музыку (query, kind
selection для подборки), alarm - умный будильник (тихая мелодия плавно
нарастает; query - своя мелодия, иначе спокойная утренняя), task - поручение
тебе самой на то время («в 8 утра расскажи погоду», «в пятницу проверь счёт
за свет») - тогда ты его выполнишь и скажешь результат вслух.
when - дата и время «YYYY-MM-DD HH:MM» (сегодняшняя дата и время - в начале
каждой реплики); «через 10 минут» посчитай сама.
- Разово или регулярно? Если просьба звучит как повторяющаяся («буди меня»,
  «включай», «по утрам», «напоминай пить таблетки»), а это не сказано прямо -
  СНАЧАЛА спроси одной фразой: «Это только завтра или каждый день / по
  будням?» - и только потом добавляй. Сказано прямо («каждый понедельник»,
  «только завтра») - не переспрашивай. repeat: {"every": day | weekdays |
  weekend | week (+ days: 0=пн..6=вс) | month | year}.
- Напоминание другому человеку (who) - в Телеграм ему (и дома вслух, если он дома).
- Добавив, подтверди коротко: что, когда, разово или регулярно.
- calendar_list - что запланировано (и ближайшие дни рождения); calendar_remove
  - убрать по id (сначала calendar_list, если id не знаешь).
- Будильник выключают «Ева, выключи» (это остановка музыки). «Ещё 10 минут» -
  новый разовый alarm через 10 минут.
- Дни рождения из семейного дерева ты объявляешь дома сама: утром в день
  рождения и накануне вечером - их в календарь добавлять не надо."""

_REPEAT = {"type": "object", "additionalProperties": False, "required": ["every"],
           "properties": {"every": {"type": "string", "enum": list(cal.EVERY)},
                          "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6},
                                   "description": "для week: дни недели, 0=пн .. 6=вс"}}}

SKILL = Skill(
    name="calendar",
    summary="календарь и планировщик: напоминания, музыка и будильник по времени, поручения, дни рождения",
    prompt=PROMPT,
    tools=[Tool(
        name="calendar_add",
        description="Поставить в календарь: напоминание, музыку, будильник или поручение себе на время, "
                    "разово или с повтором.",
        schema={"type": "object", "additionalProperties": False, "required": ["title", "when", "action"],
                "properties": {
                    "title": {"type": "string", "description": "коротко по-русски («Наутилус - Крылья», «таблетки»)"},
                    "when": {"type": "string", "description": "YYYY-MM-DD HH:MM, местное время"},
                    "action": {"type": "string", "enum": ["remind", "music", "alarm", "task"]},
                    "repeat": _REPEAT,
                    "text": {"type": "string", "description": "remind: что сказать; task: поручение тебе"},
                    "query": {"type": "string", "description": "music / alarm: что искать на YouTube Music"},
                    "kind": {"type": "string", "enum": ["track", "selection"]},
                    "notify": {"type": "string", "enum": ["home", "telegram", "both"]},
                    "who": {"type": "string", "description": "для кого напоминание, если не для того, кто просит"}}},
        handler=add, needs="known",
    ), Tool(
        name="calendar_list",
        description="Что запланировано на ближайшие дни (повторы развёрнуты) и ближайшие дни рождения.",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 60}}},
        handler=listing, needs="known",
    ), Tool(
        name="calendar_remove",
        description="Убрать событие из календаря по id (повторяющееся - целиком).",
        schema={"type": "object", "additionalProperties": False, "required": ["id"],
                "properties": {"id": {"type": "string"}}},
        handler=remove, needs="known",
    )],
)
