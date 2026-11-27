"""Skills: Eva's abilities as plug-in modules (docs/ARCHITECTURE.md).

    app/skills/<name>/__init__.py   SKILL = Skill(...)

A skill gives tools (each with the access it `needs`), an optional piece of
the prompt (`prompt`, static) and an optional `context()` (dynamic, refreshed
on a timer). The core decides which tools a Person may see and checks again
before running one, so a skill never has to.
"""
import importlib
import os
import pkgutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from core.util import log


@dataclass
class Result:
    text: str                        # goes back to Claude
    show: dict | None = None         # a presentation for the channel (screen, Telegram)
    blocks: list | None = None       # content blocks for Claude instead of the text (an image to look at)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict                     # JSON schema of the input (object)
    handler: Callable                # handler(args: dict, person) -> Result | str
    needs: str | None = "known"      # None | known | adult | admin | <photolib section>

    def spec(self):
        return {"name": self.name, "description": self.description, "input_schema": self.schema,
                "eager_input_streaming": True}


@dataclass
class Skill:
    name: str
    summary: str
    tools: list = field(default_factory=list)
    prompt: str = ""                 # static instructions, part of the cached prompt
    context: Callable | None = None  # () -> str, refreshed hourly


def _think_deeper(args, person):
    return ("Хорошо: следующий шаг делает более сильная модель. Продолжай - собери, что нужно, "
            "и ответь.")


# Not a skill of a home: part of the core, every home has it. Sonnet's way to call
# Opus in (core/llm.py _escalate); the reason goes to the log for tuning.
THINK_DEEPER = Tool(
    name="think_deeper",
    description=("Hand the next step of the answer to a stronger model. Call it BEFORE answering if any holds: "
                 "data from several sources or many numbers must be matched (a calculation, totals, comparing "
                 "periods); the data contradict each other or are too thin for a sure conclusion (one short "
                 "phrase, an unknown voice, an empty result where data should be); a conclusion about people - "
                 "who is where, who said what, who is on a photo; money, health, safety; you tried twice and "
                 "failed; you are about to say «наверное», «кажется», «скорее всего» about a fact. Simple "
                 "actions, showing and retelling what was found - yourself, without this tool."),
    schema={"type": "object", "additionalProperties": False, "required": ["reason"],
            "properties": {"reason": {"type": "string", "description": "briefly, why a stronger model is needed"}}},
    handler=_think_deeper, needs="known",
)

def _private(args, person):
    """The household's private notes (eva/PRIVATE.md): health, documents, money."""
    from core import prompt
    if not person.may("adult"):
        return "Нет доступа: это только для взрослых."
    try:
        return open(prompt.PRIVATE, encoding="utf-8").read()
    except OSError:
        return "Личных заметок нет."


# The private block was in the prompt for a known adult only - a prompt (and a cache) of
# its own. As a tool the prompt is one for all, and a child or an unknown voice still
# cannot get it: the right is checked when it runs.
PRIVATE_NOTES = Tool(
    name="private_notes",
    description=("The family's private notes: health, documents, money, all that is not for children or "
                 "strangers. Open when the question or answer touches it; only for a known adult at home."),
    schema={"type": "object", "additionalProperties": False, "properties": {}},
    handler=_private, needs="private",
)

SKILLS = []
# a picture she looked at -> a small copy of it for the conversation history
# (without any picture there she later "confessed" she had not seen it, 2026-11-20)
HISTORY_IMAGES = {}
_context = {}


def load():
    """Import every package under app/skills that defines SKILL - the ones this
    home has: profile.json "skills" lists them (absent = all). Another home
    may have no photo archive, finances or power meter."""
    from core import profile
    import skills as pkg
    for m in pkgutil.iter_modules(pkg.__path__):
        if profile.SKILLS is not None and m.name not in profile.SKILLS:
            log(f"skill {m.name}: off in the profile")
            continue
        try:
            mod = importlib.import_module(f"skills.{m.name}")
        except Exception as e:  # a broken skill must not take Eva down
            log(f"skill {m.name}: cannot load: {e}")
            continue
        if hasattr(mod, "SKILL"):
            SKILLS.append(mod.SKILL)
    refresh()
    log("skills:", ", ".join(f"{s.name} ({len(s.tools)} tools)" for s in SKILLS))


def refresh():
    for s in SKILLS:
        if s.context:
            try:
                _context[s.name] = s.context() or ""
            except Exception as e:  # backend away: keep the last good text
                log(f"skill {s.name}: context: {e}")


def refresher(every=3600):
    while True:
        time.sleep(every)
        refresh()


def prompt():
    """The skills' part of the system prompt: static instructions, then context."""
    parts = []
    for s in SKILLS:
        if s.prompt:
            parts.append(s.prompt.strip())
        if _context.get(s.name):
            parts.append(_context[s.name].strip())
    return "\n\n".join(parts)


def _tools():
    return [t for s in SKILLS for t in s.tools] + [THINK_DEEPER, PRIVATE_NOTES]


def specs_for(person):
    """The same tools for everybody: a tool's right is checked when it runs (run below).
    A list per person (47 / 31 / 7 tools) made a prompt cache per person, and every change
    of speaker wrote ~60k tokens again (~$0.25, 2026-11-26)."""
    return [t.spec() for t in _tools()]


def run(block, person):
    """A tool_use block -> (tool_result block, presentation or None)."""
    tool = next((t for t in _tools() if t.name == block.name), None)
    args = block.input if isinstance(block.input, dict) else None
    if tool is None or args is None:
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": True, "content": "bad tool call"}, None
    if not person.may(tool.needs):
        log(f"tool {tool.name}: denied for {person.name or 'unknown voice'}")
        # the reason, so she can say it (a bare "no access" made her answer "I do not know why")
        why = {"adult": "это только для взрослых", "admin": "это может только хозяин системы",
               "private": "личное - только дома, голосом", "control": "управлять домом можно только дома, голосом",
               "screen": "экран - только голосом у колонки"}.get(tool.needs, "этому человеку это не разрешено")
        if not person.known:
            why = "голос не узнан - личное и действия только для своих"
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                "content": f"Нет доступа: {why}."}, None
    # required: present and not empty - a string with text, a list with items
    # (a list of devices was taken for "missing", 2026-11-20)
    missing = [k for k in tool.schema.get("required", [])
               if args.get(k) is None or (isinstance(args[k], str) and not args[k].strip())
               or (isinstance(args[k], (list, dict)) and not args[k])]
    if missing:
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                "content": f"missing: {', '.join(missing)}"}, None
    log(f"tool {tool.name}: {args}")
    try:
        r = tool.handler(args, person)
    except Exception as e:  # the backend of a skill is down
        log(f"tool {tool.name}: {e}")
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                "content": f"Сейчас не получилось: {e}"}, None
    if isinstance(r, str):
        r = Result(r)
    return {"type": "tool_result", "tool_use_id": block.id, "content": r.blocks or r.text}, r.show


def start_refresher():
    threading.Thread(target=refresher, daemon=True).start()
