"""Skill "журнал действий": what she did, given back whole (core/actions.py,
docs/ACTIONS-PLAN.md). The history keeps short copies of her tool results,
each naming its action; this re-reads one instead of searching again - or of
"confessing" to a guess she never made."""
from core import actions
from core.skills import Result, Skill, Tool

MAX_CHARS = 40000

PROMPT = """## Your actions are kept whole in the journal

Tool results in the conversation history (searches, opened pages, data, photos) are
shortened copies marked «в истории сокращено … действие #N». That is to save
space, not a sign you did not see something: when you answered you saw the whole
result, and it is in the journal. Need a detail the copy lacks, or unsure what you
read then - re-read it: action_recall(id=N). Do not search again for what you found,
and never say «я этого не проверяла / придумала» without opening the action."""


def recall(args, person):
    try:
        a = actions.fetch(int(args["id"]))
    except Exception as e:
        return f"Журнал недоступен: {e}"
    if not a:
        return f"Действия #{args['id']} нет в журнале."
    if a["person"] and a["person"] != person.profile and not person.may("admin"):
        return "Это действие было для другого человека."
    head = (f"Действие #{a['id']}: {a['tool']} ({a['channel'] or ''}, {a['ts']:%d.%m %H:%M})"
            + (f", источник {a['source']}" if a["source"] else "")
            + (f", получен {a['source_date']}" if a["source_date"] else "")
            + f"\nЗапрос: {a['input']}\nРезультат целиком:\n")
    text = a["result"] or ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f" …[ещё {len(text) - MAX_CHARS} символов]"
    blocks = None
    if a["images"]:
        blocks = [{"type": "text", "text": head + text}] + [{"type": "image", "source": s} for s in a["images"] if s]
    return Result(head + text, blocks=blocks)


SKILL = Skill(
    name="journal",
    summary="журнал действий: перечитать целиком то, что ты уже нашла или увидела",
    prompt=PROMPT,
    tools=[Tool(
        name="action_recall",
        description=("Re-read in full the result of a past action of yours by its number from the mark "
                     "«действие #N» in the history: a page, a search, data, photos. Instead of searching "
                     "again, and before saying you did not check something."),
        schema={"type": "object", "additionalProperties": False, "required": ["id"],
                "properties": {"id": {"type": "integer", "description": "the action number from the mark in the history"}}},
        handler=recall,
        needs="known",
    )],
)
