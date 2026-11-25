"""Skill "журнал действий": what she did, given back whole (core/actions.py,
docs/ACTIONS-PLAN.md). The history keeps short copies of her tool results,
each naming its action; this re-reads one instead of searching again - or of
"confessing" to a guess she never made."""
from core import actions
from core.skills import Result, Skill, Tool

MAX_CHARS = 40000

PROMPT = """## Твои действия - целиком в журнале

В истории разговора результаты твоих инструментов (поиск, открытые страницы,
данные, фото) - сокращённые копии с пометкой «в истории сокращено …
действие #N». Это сокращение для экономии, а не след того, что ты чего-то не
видела: отвечая тогда, ты видела результат целиком, и он лежит в журнале.
Нужна деталь, которой нет в копии, или сомневаешься, что тогда прочитала, -
перечитай действие: action_recall(id=N). Не ищи заново то, что уже нашла, и
не говори «я этого не проверяла / придумала», не открыв действие."""


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
        description="Перечитать целиком результат твоего прошлого действия по его номеру из пометки "
                    "«действие #N» в истории: страницу, поиск, данные, фото. Вместо повторного поиска и "
                    "прежде чем сказать, что ты чего-то не проверяла.",
        schema={"type": "object", "additionalProperties": False, "required": ["id"],
                "properties": {"id": {"type": "integer", "description": "номер действия из пометки в истории"}}},
        handler=recall,
        needs="known",
    )],
)
