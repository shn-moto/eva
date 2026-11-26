"""Skill "музыка": YouTube Music at home through the Station (app/music.py,
the service on shn-linux). Ported from the old assistant.py: a track, a
selection («подборка», «песни …»), a whole album; pause, next, louder."""
from core.skills import Skill, Tool
import music


def play(args, person):
    q = args["query"].strip()
    kind = args.get("kind") or "track"
    n = {"track": 1, "selection": 8, "album": 1}.get(kind, 1)
    try:
        found = music.search(q, "album" if kind == "album" else "track", n)
    except (OSError, ValueError) as e:
        return f"Музыкальный сервис недоступен: {e}"
    if not found:
        return f"Ничего не нашла по запросу «{q}»."
    music.PLAYER.play(found)
    first = found[0]
    rest = f", в очереди ещё {len(found) - 1}" if len(found) > 1 else ""
    return (f"Включила: {first.get('title')}" + (f" - {first['artist']}" if first.get("artist") else "")
            + rest + ". На телевизоре - плеер. Вслух про музыку ничего не говори; если ответить больше "
            "нечего - ответь <молчу>.")


def control(args, person):
    a = args["action"]
    p = music.PLAYER
    if not p.active():
        return "Музыка сейчас не играет."
    if a == "pause":
        p.pause(True)
    elif a == "resume":
        p.pause(False)
    elif a in ("next", "previous"):
        if not p.next(1 if a == "next" else -1):
            return "Дальше в очереди ничего нет." if a == "next" else "Это первый трек."
    elif a == "stop":
        p.stop()
        return "Музыку выключила. Вслух не подтверждай; если ответить больше нечего - ответь <молчу>."
    elif a in ("louder", "quieter"):
        p.louder(a == "louder")
    return p.describe()


PROMPT = """## Музыка

music_play включает музыку дома (YouTube Music через колонку): kind track - одна
песня, selection - подборка («включи что-нибудь из Queen», «песни для детей»),
album - альбом целиком. music_control - пауза, дальше, назад, громче, тише, стоп,
и что сейчас играет. Пока музыка играет, тебя слышно поверх неё. Включив или выключив
музыку, ничего не говори про это: плеер на телевизоре и так видно, а озвучка стоит денег.
Если кроме музыки ответить нечего - ответь <молчу>."""

SKILL = Skill(
    name="music",
    summary="музыка дома: включить песню, подборку или альбом, пауза, дальше, громкость",
    prompt=PROMPT,
    tools=[Tool(
        name="music_play",
        description="Включить музыку дома: песню, подборку или альбом. query - исполнитель и/или название "
                    "как ищут на YouTube Music (лучше латиницей, если исполнитель зарубежный).",
        schema={"type": "object", "additionalProperties": False, "required": ["query"],
                "properties": {"query": {"type": "string"},
                               "kind": {"type": "string", "enum": ["track", "selection", "album"]}}},
        handler=play,
        needs=None,
    ), Tool(
        name="music_control",
        description="Управление музыкой: pause, resume, next, previous, louder, quieter, stop; status - что "
                    "играет и что дальше.",
        schema={"type": "object", "additionalProperties": False, "required": ["action"],
                "properties": {"action": {"type": "string", "enum": ["pause", "resume", "next", "previous",
                                                                    "louder", "quieter", "stop", "status"]}}},
        handler=control,
        needs=None,
    )],
)
