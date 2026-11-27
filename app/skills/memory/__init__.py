"""Skill "память": long-term memory search (assistant_memory on shn-linux)."""
import memory
from core.skills import Skill, Tool

PROMPT = """## Memory

Everything the family and you said before is in long-term memory. Asked what was
discussed, to remind what was said or what someone asked («что мы обсуждали
вчера», «я говорил про…», «напомни, что…») - call memory_search and answer from what
it finds; nothing found - say so, never make it up."""


def search(args, person):
    return memory.search(args["query"])


SKILL = Skill(
    name="memory",
    summary="долговременная память: что семья и Ева говорили раньше",
    prompt=PROMPT,
    tools=[Tool(
        name="memory_search",
        description=("Semantic search in long-term memory: everything the family and Eva said before, with "
                     "dates. For «что мы обсуждали», «напомни», «я говорил про»."),
        schema={"type": "object", "additionalProperties": False, "required": ["query"],
                "properties": {"query": {"type": "string", "description": "the topic in your own words"}}},
        handler=search,
        needs="known",
    )],
)
