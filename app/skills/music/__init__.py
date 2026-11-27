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


PROMPT = """## Music

kind: track - one song, selection - a mix («включи что-нибудь из Queen», «песни для
детей»), album - a whole album. While music plays you are heard over it. Turning
music on or off, say nothing about it: the player on the TV shows it, and speech
costs money. If there is nothing else to answer - answer <молчу>."""

SKILL = Skill(
    name="music",
    summary="музыка дома: включить песню, подборку или альбом, пауза, дальше, громкость",
    prompt=PROMPT,
    tools=[Tool(
        name="music_play",
        description=("Play music at home: a song, a mix or an album. query - artist and/or title as searched "
                     "on YouTube Music (Latin letters for a foreign artist)."),
        schema={"type": "object", "additionalProperties": False, "required": ["query"],
                "properties": {"query": {"type": "string"},
                               "kind": {"type": "string", "enum": ["track", "selection", "album"]}}},
        handler=play,
        needs=None,
    ), Tool(
        name="music_control",
        description=("Music control: pause, resume, next, previous, louder, quieter, stop; status - what plays "
                     "and what is next."),
        schema={"type": "object", "additionalProperties": False, "required": ["action"],
                "properties": {"action": {"type": "string", "enum": ["pause", "resume", "next", "previous",
                                                                    "louder", "quieter", "stop", "status"]}}},
        handler=control,
        needs=None,
    )],
)
