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
    return [t for s in SKILLS for t in s.tools]


def specs_for(person):
    return [t.spec() for t in _tools() if person.may(t.needs)]


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
