"""Skill "память": long-term memory search (assistant_memory on shn-linux)."""
import memory
from core.skills import Skill, Tool

PROMPT = """## Память

Всё, что вы с семьёй говорили раньше, хранится в долговременной памяти. Если
спрашивают, что обсуждали, просят напомнить, о чём говорили или что кто-то
просил («что мы обсуждали вчера», «я говорил про…», «напомни, что…») - вызови
memory_search и отвечай по найденному; не нашлось - так и скажи, не выдумывай."""


def search(args, person):
    return memory.search(args["query"])


SKILL = Skill(
    name="memory",
    summary="долговременная память: что семья и Ева говорили раньше",
    prompt=PROMPT,
    tools=[Tool(
        name="memory_search",
        description="Семантический поиск по долговременной памяти: всё, что семья и Ева говорили "
                    "раньше, с датами. Для вопросов «что мы обсуждали», «напомни», «я говорил про».",
        schema={"type": "object", "additionalProperties": False, "required": ["query"],
                "properties": {"query": {"type": "string", "description": "тема поиска своими словами"}}},
        handler=search,
        needs="known",
    )],
)
