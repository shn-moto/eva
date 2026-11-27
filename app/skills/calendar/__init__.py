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


PROMPT = """## Calendar and scheduler

calendar_add actions: remind (notify: home - aloud at home, telegram, both); music
(query; kind selection for a mix); alarm - a smart alarm (a quiet tune slowly
rising; query - their own tune, else a calm morning one); task - an errand for you
at that time («в 8 утра расскажи погоду», «в пятницу проверь счёт за свет») - you
do it then and say the result aloud.
when - "YYYY-MM-DD HH:MM" (today's date and time are at the start of every phrase);
work out «через 10 минут» yourself.
- Once or recurring? If the request sounds recurring («буди меня», «включай», «по
  утрам», «напоминай пить таблетки») and it is not said - FIRST ask in one sentence
  «Это только завтра или каждый день / по будням?», only then add. Said plainly
  («каждый понедельник», «только завтра») - do not ask. repeat: {"every": day |
  weekdays | weekend | week (+ days: 0=Mon..6=Sun) | month | year}.
- A reminder for another person (who) goes to their Telegram (and aloud if home).
- After adding, confirm briefly: what, when, once or recurring.
- calendar_remove needs the id: calendar_list first if you do not know it.
- The alarm is stopped by «Ева, выключи» (stops the music). «Ещё 10 минут» - a new
  one-off alarm in 10 minutes.
- Birthdays from the family tree you announce at home yourself (that morning and the
  evening before): do not add them to the calendar."""

_REPEAT = {"type": "object", "additionalProperties": False, "required": ["every"],
           "properties": {"every": {"type": "string", "enum": list(cal.EVERY)},
                          "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6},
                                   "description": "for week: weekdays, 0=Mon .. 6=Sun"}}}

SKILL = Skill(
    name="calendar",
    summary="календарь и планировщик: напоминания, музыка и будильник по времени, поручения, дни рождения",
    prompt=PROMPT,
    tools=[Tool(
        name="calendar_add",
        description=("Put in the calendar: a reminder, music, an alarm or an errand for yourself at a time, "
                     "once or recurring."),
        schema={"type": "object", "additionalProperties": False, "required": ["title", "when", "action"],
                "properties": {
                    "title": {"type": "string", "description": "short, in Russian («Наутилус - Крылья», «таблетки»)"},
                    "when": {"type": "string", "description": "YYYY-MM-DD HH:MM, local time"},
                    "action": {"type": "string", "enum": ["remind", "music", "alarm", "task"]},
                    "repeat": _REPEAT,
                    "text": {"type": "string", "description": "remind: what to say; task: the errand for you"},
                    "query": {"type": "string", "description": "music / alarm: what to search on YouTube Music"},
                    "kind": {"type": "string", "enum": ["track", "selection"]},
                    "notify": {"type": "string", "enum": ["home", "telegram", "both"]},
                    "who": {"type": "string", "description": "whom the reminder is for, if not the one asking"}}},
        handler=add, needs="known",
    ), Tool(
        name="calendar_list",
        description="What is planned for the coming days (repeats expanded) and the nearest birthdays.",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 60}}},
        handler=listing, needs="known",
    ), Tool(
        name="calendar_remove",
        description="Remove an event from the calendar by id (a recurring one - entirely).",
        schema={"type": "object", "additionalProperties": False, "required": ["id"],
                "properties": {"id": {"type": "string"}}},
        handler=remove, needs="known",
    )],
)
