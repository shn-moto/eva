"""One Claude tool loop for every channel: streamed for the voice (text
chunks go to a callback as they come), whole for text channels. Tools are the
skills' tools the Person may use; presentations from tools go to `on_show`.

Adaptive thinking at low effort. It was between_tools (thinking off) for the
first word sooner, but then she thought aloud in the spoken text ("голос не
узнан, так что личное не рассказываю…"); measured 2026-11-20 on her own
prompt: adaptive came first 0.8-0.96 s vs 1.4 s, and counted ages right.
Thinking of a finished turn is not kept in the history (see _stored). A
refusal is logged.
"""
import os
import time

import anthropic

from core import cost, skills
from core import profile
from core.util import log, secrets

MODEL = profile.tune("llm.model", "claude-sonnet-5-5")   # user choice 2026-11-18: ~0.4 s faster than Opus
EFFORT = profile.tune("llm.effort", "low")
CLIENT = anthropic.Anthropic(api_key=secrets()["ANTHROPIC_API_KEY"], max_retries=1, timeout=30)
ROUNDS = profile.tune("llm.rounds", 8)                           # tool rounds in one answer (an agent: look, then act, then report)
# Weighing what the tools gave and deciding the next step is the agent's work:
# Opus at medium (Иван, 2026-11-20 - Sonnet low took one short word for "Маша
# is at home"). The voice keeps Sonnet low for its first round (the first word,
# small talk, "сейчас гляну"); once a tool has run, the rounds go to Opus.
# Text channels: Opus medium all the way, no one waits for a first word there.
AGENT_MODEL = profile.tune("llm.agent_model", "claude-opus-5-5")
AGENT_EFFORT = profile.tune("llm.agent_effort", "medium")
# ...but only when there is something to think over. A simple action is done
# by Sonnet to the end: «выключи музыку» went Sonnet -> tool -> Opus, and Opus
# (its own cache, cold) wrote 26k tokens to say "Выключила музыку" - $0.16
# (2026-11-21). Opus comes in after a tool that brings data to weigh; any tool
# not listed here counts as such.
LIGHT_TOOLS = profile.tune("llm.light_tools", {"music_play", "music_control", "screen", "panel", "avatar", "say_at_home", "send_message",
               "eva_spend", "photo_indexing", "show_html", "show_feed",
               "home_api_index", "home_api_get",
               "file_print", "print_photos", "print_text", "print_url", "printer_status", "print_cancel"})


def _escalate(message, params):
    """Params for the next round: Opus once a tool needs weighing, else as they are."""
    if params["model"] == AGENT_MODEL:
        return params
    used = {b.name for b in message.content if b.type == "tool_use"}
    if used <= LIGHT_TOOLS:
        return params
    log(f"to {AGENT_MODEL}: {', '.join(sorted(used - LIGHT_TOOLS))}")
    return dict(params, model=AGENT_MODEL, output_config={"effort": AGENT_EFFORT})


KEEP_RESULT = profile.tune("llm.keep_result", 1500)                   # chars of a tool result kept in the conversation history (4000 was 60 % of it)


# The internet: Anthropic's server tools (run on their side, within the same
# answer). For the people she knows; a guest's question does not cost searches.
# The place makes prices and shops local (profile household.location).
WEB = profile.tune("llm.web", True)
WEB_SEARCH_USES = profile.tune("llm.web_search_uses", 5)
WEB_FETCH_USES = profile.tune("llm.web_fetch_uses", 3)


def _web_tools(person):
    if not WEB or not person.known:
        return []
    search = {"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCH_USES}
    if profile.LOCATION:
        search["user_location"] = dict(profile.LOCATION, type="approximate")
    return [search, {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": WEB_FETCH_USES,
                     "max_content_tokens": profile.tune("llm.web_fetch_tokens", 8000)}]


def _params(system, person, max_tokens, effort=None, model=None):
    tools = skills.specs_for(person) + _web_tools(person)
    # Two cache breakpoints. (1) The system prompt (with the tools before it):
    # the same for an hour, read at a tenth of the price. With one breakpoint
    # at the very end only, every change at the start of the history missed
    # the whole cache: 83 replies wrote 1.03 M tokens and read 0.81 M, a
    # reply 1 min after another rewrote 32 k (2026-11-20, ~$5 a day).
    # (2) The top-level one: the conversation, up to its last message - for an
    # hour too: at home a request comes after a pause of 5-60 min, and with 5
    # minutes nearly every one wrote the whole history again (2026-11-21).
    p = dict(model=model or MODEL, max_tokens=max_tokens,
             system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
             thinking={"type": "adaptive"}, output_config={"effort": effort or EFFORT},
             cache_control={"type": "ephemeral", "ttl": "1h"})
    if tools:
        p["tools"] = tools
    return p


def _stored(content):
    """An assistant turn as the history keeps it: text and tool calls (thinking
    of a finished turn is not needed and is dropped)."""
    out = []
    for b in content:
        if b.type == "text" and b.text:
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            inp = b.input
            if b.name == "show_html" and isinstance(inp, dict) and len(inp.get("html", "")) > 300:
                # the page itself is on the TV; kept whole, a chart was ~8k chars of every request
                inp = dict(inp, html=f"[страница {len(inp['html']) // 1000} КБ, показана]")
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
    return out


def _tool_round(final, msgs, person, on_show, trace=None, record=None):
    msgs.append({"role": "assistant", "content": final.content})
    results = []
    for b in final.content:
        if b.type == "tool_use":
            res, show = skills.run(b, person)
            results.append(res)
            if trace is not None:
                trace.append(f"{b.name}({', '.join(f'{k}={v}' for k, v in b.input.items())})"
                             f"{' - ошибка' if res.get('is_error') else ''}: {str(res.get('content'))[:300]}")
            if show and on_show:
                on_show(show)
    msgs.append({"role": "user", "content": results})
    if record is not None:
        # the real calls and results go into the history: a note in brackets
        # after her own text she took for her own invention and "confessed" that
        # she had made the search up (Telegram, 2026-11-19)
        record.append({"role": "assistant", "content": _stored(final.content)})
        record.append({"role": "user", "content": [dict(r, content=_kept(r.get("content", ""))) for r in results]})


def _cut(text):
    """A long result shortened for the history, and said so. Without the mark she
    read her own cut result on the next turn, decided she had answered without
    the data and "confessed" to a guess she never made (2026-11-23, sensors;
    the same with a photo search on 2026-11-19)."""
    if len(text) <= KEEP_RESULT:
        return text
    return (text[:KEEP_RESULT] + f" …[в истории сокращено: {KEEP_RESULT} из {len(text)} символов. "
            "Когда ты отвечала, результат был у тебя целиком - твой ответ на нём основан. "
            "Нужны подробности, которых здесь нет, - запроси снова]")


def _kept(content):
    """A tool result as the history keeps it: text, cut (and marked as cut); a
    picture she looked at as a small copy (~50 tokens; the full one would be sent
    again with every phrase)."""
    if isinstance(content, list):
        out = []
        for b in content:
            if b.get("type") == "text":
                out.append({"type": "text", "text": _cut(b["text"])})
            elif b.get("type") == "image":
                small = skills.HISTORY_IMAGES.pop(b["source"].get("data", ""), None)
                if small:
                    out.append({"type": "image", "source": dict(b["source"], data=small)})
                else:
                    out.append({"type": "text", "text": "[картинка: ты её видела целиком, в истории не хранится]"})
        return out or "…"
    return _cut(str(content))


def prepare(history, person):
    """The history for a request: with no tools for this person (a guest) the
    API refuses tool blocks - they are turned into text then."""
    if skills.specs_for(person):
        return list(history)
    out = []
    for m in history:
        c = m["content"]
        if isinstance(c, str):
            out.append(m)
            continue
        text = []
        for b in c:
            if b.get("type") == "text":
                text.append(b["text"])
            elif b.get("type") == "tool_use":
                text.append(f"[вызов {b['name']}]")
            elif b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "[картинка]") for x in c)
                text.append(f"[результат: {str(c)[:200]}]")
        out.append({"role": m["role"], "content": " ".join(text) or "…"})
    return out


# How much of a conversation she keeps: by volume, not by time (Иван,
# 2026-11-20) - a quiet day is remembered for days, a busy one for hours.
# ~40k characters is ~15k tokens a request, read mostly from the cache.
HISTORY_CHARS = profile.tune("llm.history_chars", 24000)   # 40000 was ~10k tokens a request
TRIM_TO = profile.tune("llm.trim_to", 0.6)


def _size(m):
    c = m["content"]
    if isinstance(c, str):
        return len(c)
    n = 0
    for b in c:
        if b.get("type") == "text":
            n += len(b["text"])
        elif b.get("type") == "tool_use":
            n += len(str(b.get("input"))) + 40
        elif b.get("type") == "tool_result":
            n += sum(len(x.get("text", "")) + (1000 if x.get("type") == "image" else 0) for x in b["content"])                 if isinstance(b.get("content"), list) else len(str(b.get("content")))
        else:
            n += 1000                       # an image
    return n


def trim(history, budget=HISTORY_CHARS):
    """The newest messages that fit in `budget` characters, cut so that it
    starts with a user's own words (never with a tool result: its call would
    be gone)."""
    # over the budget, it is cut to TRIM_TO of it at once, not by one message
    # every turn: the start of the history then stays the same for many turns
    # and its cache keeps working (a cut start is a new prefix, all rewritten)
    if sum(_size(m) for m in history) > budget:
        total, keep = 0, 0
        for m in reversed(history):
            total += _size(m)
            if total > budget * TRIM_TO and keep:
                break
            keep += 1
        del history[:-keep or None]
    while history and not (history[0]["role"] == "user" and isinstance(history[0]["content"], str)):
        del history[0]


def trace_note(trace):
    """Kept for logs; the history has the real tool blocks now (see _tool_round)."""
    return ("\n[служебно, не произносить - ты вызвала: " + "; ".join(trace) + "]") if trace else ""


def stream(system, history, person, on_chunk, on_show=None, on_tool=None, max_tokens=4096, trace=None, record=None):
    """on_chunk(text) -> False to stop (she was interrupted). on_tool(name):
    a tool call begins - the moment to say "секунду, посмотрю" instead of
    keeping the room in silence while the tool and the next round run.
    record: gets the tool rounds (calls and results) for the history.
    -> {"final": Message|None, "cut": bool, "first": seconds to first text}"""
    msgs, t0, first = prepare(history, person), time.time(), None
    params = _params(system, person, max_tokens)
    for _ in range(ROUNDS):
        cost.check()
        with CLIENT.messages.stream(messages=msgs, **params) as s:
            for ev in s:
                if ev.type == "content_block_start" and ev.content_block.type in ("tool_use", "server_tool_use"):
                    if on_tool:
                        on_tool(ev.content_block.name)
                    continue
                if ev.type != "text":
                    continue
                if first is None:
                    first = time.time() - t0
                if on_chunk(ev.text) is False:
                    try:                 # a cut answer is billed too: the usage so far
                        cost.add(params["model"], s.current_message_snapshot.usage, "voice")
                    except Exception:
                        pass
                    return {"final": None, "cut": True, "first": first}
            final = s.get_final_message()
        usd = cost.add(params["model"], final.usage, "voice")
        log(f"cost {params['model'].replace('claude-', '')} ${usd:.4f}: read {final.usage.cache_read_input_tokens} "
            f"written {final.usage.cache_creation_input_tokens} in {final.usage.input_tokens} "
            f"out {final.usage.output_tokens}")
        if final.stop_reason == "pause_turn":
            # a long web search paused on the server's side: the same turn goes
            # back as it is and the server goes on with it
            msgs.append({"role": "assistant", "content": final.content})
            continue
        if final.stop_reason != "tool_use":
            break
        _tool_round(final, msgs, person, on_show, trace, record)
        params = _escalate(final, params)
        on_chunk(" ")                    # text before and after a tool call stays apart
    if final.stop_reason == "refusal":
        log("refusal:", getattr(final.stop_details, "category", None))
    return {"final": final, "cut": False, "first": first}


def final_text(message):
    return "".join(b.text for b in message.content if b.type == "text").strip()


def complete(system, history, person, on_show=None, max_tokens=2048, trace=None, record=None):
    """The whole answer as text (text channels)."""
    msgs, parts = prepare(history, person), []
    params = _params(system, person, max_tokens)          # Sonnet; Opus only when there is data to weigh
    for _ in range(ROUNDS):
        cost.check()
        r = CLIENT.messages.create(messages=msgs, **params)
        usd = cost.add(params["model"], r.usage, "chat")
        log(f"cost {params['model'].replace('claude-', '')} ${usd:.4f}: read {r.usage.cache_read_input_tokens} "
            f"written {r.usage.cache_creation_input_tokens} in {r.usage.input_tokens} out {r.usage.output_tokens}")
        part = final_text(r)
        if part:
            parts.append(part)
        if r.stop_reason == "pause_turn":            # a web search goes on (see stream)
            msgs.append({"role": "assistant", "content": r.content})
            continue
        if r.stop_reason != "tool_use":
            break
        _tool_round(r, msgs, person, on_show, trace, record)
        params = _escalate(r, params)
    if record is not None:
        record.append({"role": "assistant", "content": final_text(r) or "…"})
    # only the last round's words: the text before a tool call is her "сейчас
    # посмотрю" or thinking aloud ("…так что сначала найду, а поздравлю в
    # ответе" came to Telegram, 2026-11-19); the voice speaks it, a chat does not
    return final_text(r) or "\n\n".join(parts)
