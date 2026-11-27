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
import contextlib
import os
import re
import threading
import time

import anthropic

from core import actions, cost, skills
from core import profile
from core.util import log, secrets

MODEL = profile.tune("llm.model", "claude-sonnet-5-5")   # user choice 2026-11-18: ~0.4 s faster than Opus
EFFORT = profile.tune("llm.effort", "low")
CLIENT = anthropic.Anthropic(api_key=secrets()["ANTHROPIC_API_KEY"], max_retries=1, timeout=30)
ROUNDS = profile.tune("llm.rounds", 8)                           # tool rounds in one answer (an agent: look, then act, then report)
# The model ladder (Иван, 2026-11-25): simple commands - the local router (no Claude);
# the rest - Sonnet low, which also weighs what its tools brought; Opus medium only
# when _escalate finds a reason (Sonnet asks, or a rule below says so). Text channels
# take the same ladder.
AGENT_MODEL = profile.tune("llm.agent_model", "claude-opus-5-5")
AGENT_EFFORT = profile.tune("llm.agent_effort", "medium")
# Tools after which nothing needs weighing (an action done, a picture shown): they
# never count towards an escalation. «выключи музыку» once went Sonnet -> tool -> Opus,
# and Opus (cold cache) wrote 26k tokens to say "Выключила музыку" - $0.16 (2026-11-21).
LIGHT_TOOLS = profile.tune("llm.light_tools", {"music_play", "music_control", "screen", "panel", "avatar", "say_at_home", "send_message",
               "eva_spend", "photo_indexing", "show_html", "show_feed", "weather",
               "home_api_index", "home_api_get",
               "file_print", "print_photos", "print_text", "print_url", "printer_status", "print_cancel",
               "print_page", "print_screen",
               # drawing: she sees the picture and says one phrase - Sonnet is enough; after
               # picture_draw Opus came in with a cold cache and spent the day's limit (2026-11-25)
               "picture_draw", "picture_redraw", "picture_save",
               "calendar_add", "calendar_list", "calendar_remove", "private_notes"})


# Opus does not come in just because a tool brought data: Sonnet (warm cache, ~$0.01 a
# round) weighs it. Opus (cold cache, $0.25-0.45 a call) comes in when one of these says
# so - Sonnet's own word is one of them, not the only one, since a model that thinks it
# can always cope never asks (Иван, 2026-11-25):
#  - she asks: the tool think_deeper (core/skills.py) with her reason;
#  - a tool whose result is a judgement about people from thin evidence (HEAVY_TOOLS):
#    Sonnet low took one short word for "Маша is at home" (2026-11-20);
#  - the person corrects her ("не так", "ты ошиблась", "перепроверь") - she was wrong once;
#  - a long chain: the third round of data tools in one answer - she is wandering;
#  - tools failing twice in one answer;
#  - much data to weigh at once (BIG_RESULT characters from data tools in one round).
# Sonnet's effort stays as it is: a mid-conversation effort change drops the messages
# cache just as a model change does (API docs, 2026-11-25).
HEAVY_TOOLS = profile.tune("llm.heavy_tools", {"who_heard"})
BIG_RESULT = profile.tune("llm.big_result", 15000)
CHAIN = profile.tune("llm.chain", 3)
CORRECTION = re.compile(r"(не так|неправильн|ошиб(лась|ка)|не т(о|от|а|у|е)\b|не поняла|не права|перепровер|проверь ещ|"
                        r"подумай|посчитай точн|уверена\?|ерунд|чушь)", re.I)


def _turn_ctx(msgs):
    """What an answer starts with, for _escalate: was she just corrected?"""
    last = msgs[-1]["content"] if msgs else ""
    text = last if isinstance(last, str) else " ".join(b.get("text", "") for b in last if isinstance(b, dict))
    return {"rounds": 0, "errors": 0, "correction": bool(CORRECTION.search(text))}


def _escalate(message, params, results=(), turn=None):
    """Params for the next round: Opus when there is a reason (above), else as they are."""
    if params["model"] == AGENT_MODEL:
        return params
    turn = turn if turn is not None else {"rounds": 0, "errors": 0, "correction": False}
    uses = [b for b in message.content if b.type == "tool_use"]
    used = {b.name for b in uses}
    data = used - LIGHT_TOOLS - {"think_deeper"}
    turn["errors"] += sum(1 for r in results if r.get("is_error"))
    if data:
        turn["rounds"] += 1
    size = sum(len(str(r.get("content", ""))) for b, r in zip(uses, results) if b.name in data)
    why = []
    for b in uses:
        if b.name == "think_deeper":
            why.append(f"she asked: {(b.input or {}).get('reason', '')[:120]}")
    if data & HEAVY_TOOLS:
        why.append(f"heavy tool {', '.join(sorted(data & HEAVY_TOOLS))}")
    if data and turn["correction"]:
        why.append("she was corrected")
    if turn["rounds"] >= CHAIN:
        why.append(f"round {turn['rounds']} of data tools")
    if turn["errors"] >= 2:
        why.append(f"{turn['errors']} tool errors")
    if size > BIG_RESULT:
        why.append(f"{size} chars of data")
    if not why:
        if data:
            log(f"sonnet weighs: {', '.join(sorted(data))} ({size} chars)")
        return params
    log(f"to {AGENT_MODEL}: {'; '.join(why)}")
    # Opus with the 5-minute cache, not the hour: it comes in mid-turn, once in an hour
    # or two, and its cache of the conversation ends inside that turn (full images and
    # results, later cut in the history) - the next Opus call never read it; 1 h writes
    # cost 2x the input, 5 min 1.25x. Within one turn its next rounds still read it.
    # (2026-11-25: 3 Opus calls wrote 140k tokens, read 26k)
    five = {"type": "ephemeral", "ttl": "5m"}
    return dict(params, model=AGENT_MODEL, output_config={"effort": AGENT_EFFORT}, cache_control=five,
                system=[dict(params["system"][0], cache_control=five)])


# chars of a tool result kept in the conversation history. 1500 cut what she needed
# later and she lost the thread (2026-11-24): with the 1-hour cache a bigger history
# costs little - it is read at a tenth of the price, written again only when trimmed
KEEP_RESULT = profile.tune("llm.keep_result", 4000)       # whole results stay in the journal (action_recall)


# The internet: Anthropic's server tools (run on their side, within the same
# answer). For the people she knows; a guest's question does not cost searches.
# The place makes prices and shops local (profile household.location).
WEB = profile.tune("llm.web", True)
WEB_SEARCH_USES = profile.tune("llm.web_search_uses", 5)
WEB_FETCH_USES = profile.tune("llm.web_fetch_uses", 3)


def _web_tools(person):
    if not WEB:
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


FETCH_SHORT = profile.tune("llm.fetch_short", 1500)       # chars of a fetched page in the history (whole in the journal)


def _server_result(use, res, sources):
    """What a server tool gave -> (whole text, short text, source, its date)."""
    if res is None:
        return "результата нет", "результата нет", None, None
    c = getattr(res, "content", None)
    if getattr(c, "type", "").endswith("_error"):
        t = "ошибка: " + str(getattr(c, "error_code", ""))
        return t, t, None, None
    if res.type == "web_search_tool_result" and isinstance(c, list):
        lines = [f"{getattr(x, 'title', '')} - {getattr(x, 'url', '')}" + (f" ({x.page_age})" if getattr(x, "page_age", None) else "")
                 for x in c]
        t = "Найдено:\n" + "\n".join(lines)
        return t, t, None, None
    if res.type == "web_fetch_tool_result":
        doc = getattr(c, "content", None)
        src = getattr(doc, "source", None)
        body = getattr(src, "data", "") if isinstance(getattr(src, "data", None), str) else ""
        url, when = getattr(c, "url", "") or "", getattr(c, "retrieved_at", None)
        head = f"Открыта страница {url}" + (f" (получена {when})" if when else "") + f": {getattr(doc, 'title', '') or ''}\n"
        short = head + body[:FETCH_SHORT] + (f" …[страница длиннее: {len(body)} символов]" if len(body) > FETCH_SHORT else "")
        return head + body, short, url, when
    out = getattr(c, "stdout", None)
    if out:
        return "Вывод:\n" + str(out), "Вывод:\n" + str(out), None, None
    if sources:
        t = "Поиск выполнен (результат у Anthropic зашифрован). Источники ответа:\n" + "\n".join(sources)
        return t, t, None, None
    return "Выполнено.", "Выполнено.", None, None


def _server_pairs(content, person=None, channel=None):
    """Anthropic's server tools that ran inside this answer (the web search, a
    page fetched, the code that filters search results) -> tool call + result
    pairs for the history, the whole results into the action journal. Without
    them the history kept only her text and she "confessed" she had made the
    news up (2026-11-25 06:59 / 09:07); with a page cut silently to its first
    lines, a week's forecast she had read (10:07)."""
    uses = [b for b in content if b.type == "server_tool_use"]
    if not uses:
        return []
    results = {getattr(b, "tool_use_id", None): b for b in content
               if b.type.endswith("_tool_result") and b.type != "tool_result"}
    sources = []
    for b in content:
        for ct in (getattr(b, "citations", None) or []):
            src = f"{getattr(ct, 'title', '') or ''} - {getattr(ct, 'url', '') or ''}"
            if getattr(ct, "url", None) and src not in sources:
                sources.append(src)
    out = []
    for i, u in enumerate(uses):
        tid = "toolu_" + u.id.split("_", 1)[-1]
        inp = u.input if isinstance(u.input, dict) else {}
        whole, short, src, when = _server_result(u, results.get(u.id), sources if i == len(uses) - 1 else [])
        aid = actions.record(channel, getattr(person, "profile", None), u.name, inp, whole, source=src, source_date=when)
        if "code" in inp:
            inp = {"code": str(inp["code"])[:400]}
        out.append({"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": u.name, "input": inp}]})
        out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                  "content": _kept(short, aid, len(whole))}]})
    return out


def _tool_round(final, msgs, person, on_show, trace=None, record=None, channel=None):
    msgs.append({"role": "assistant", "content": final.content})
    results, aids = [], []
    for b in final.content:
        if b.type == "tool_use":
            res, show = skills.run(b, person)
            results.append(res)
            # the whole result into the journal (a re-read of one is not journalled again)
            text, images = actions.blocks_text(res.get("content"))
            aids.append(None if b.name == "action_recall" else actions.record(
                channel, person.profile, b.name, b.input, text, images or None))
            if trace is not None:
                trace.append(f"{b.name}({', '.join(f'{k}={v}' for k, v in b.input.items())})"
                             f"{' - ошибка' if res.get('is_error') else ''}: {str(res.get('content'))[:300]}")
            if show and on_show:
                on_show(show)
    msgs.append({"role": "user", "content": results})
    _record_round(final, results, aids, record)
    return results


def _record_round(final, results, aids, record):
    if record is not None:
        # the real calls and results go into the history: a note in brackets
        # after her own text she took for her own invention and "confessed" that
        # she had made the search up (Telegram, 2026-11-19)
        record.append({"role": "assistant", "content": _stored(final.content)})
        record.append({"role": "user", "content": [dict(r, content=_kept(r.get("content", ""), aid))
                                                   for r, aid in zip(results, aids)]})


def _cut(text, aid=None, whole=None):
    """A long result shortened for the history, and said so. Without the mark she
    read her own cut result on the next turn, decided she had answered without
    the data and "confessed" to a guess she never made (2026-11-23, sensors;
    the same with a photo search on 2026-11-19)."""
    whole = whole or len(text)
    if len(text) <= KEEP_RESULT and whole <= len(text):
        return text
    kept = text[:KEEP_RESULT]
    where = (f"целиком - действие #{aid}, его можно перечитать: action_recall(id={aid})" if aid
             else "нужны подробности, которых здесь нет, - запроси снова")
    return (kept + f" …[в истории сокращено: {len(kept)} из {whole} символов. Отвечая тогда, ты видела "
            f"результат целиком - твой ответ на нём основан; {where}]")


def _kept(content, aid=None, whole=None):
    """A tool result as the history keeps it: text, cut (and marked as cut); a
    picture she looked at as a 512 px copy (~330 tokens; the full one would be sent
    again with every phrase)."""
    if isinstance(content, list):
        out = []
        for b in content:
            if b.get("type") == "text":
                out.append({"type": "text", "text": _cut(b["text"], aid)})
            elif b.get("type") == "image":
                small = skills.HISTORY_IMAGES.pop(b["source"].get("data", ""), None)
                if small:
                    out.append({"type": "image", "source": dict(b["source"], data=small)})
                    # a 200 px copy made her take back what she had read on the full one
                    # (a tyre size "not readable" a minute after she read it, 2026-11-24)
                    out.append({"type": "text", "text": "[в истории - уменьшенная копия; отвечая, ты видела картинку "
                                                        "целиком: что ты тогда на ней прочитала - верно"
                                                        + (f"; целиком - действие #{aid}" if aid else "") + "]"})
                else:
                    out.append({"type": "text", "text": "[картинка: ты её видела целиком, в истории не хранится"
                                                        + (f"; целиком - действие #{aid}" if aid else "") + "]"})
        return out or "…"
    return _cut(str(content), aid, whole)


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
# 24000 lost the thread within minutes; 120000 was 40k tokens - 58% of a 69k prefix
# that every cold cache write paid again ($0.28 each, 2026-11-27). 60000 is ~20k
# tokens; older turns are still in long-term memory (memory_search).
HISTORY_CHARS = profile.tune("llm.history_chars", 60000)
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
    msgs, t0 = prepare(history, person), time.time()
    params = _params(system, person, max_tokens)
    # the limit is checked when a turn begins, not between its rounds: a turn
    # cut after its tool ran printed a photo and never said a word (2026-11-25)
    cost.check()
    LAST_VOICE[0] = time.time()
    with _turn():
        return _stream(msgs, params, person, on_chunk, on_show, on_tool, trace, record, t0)


def _stream(msgs, params, person, on_chunk, on_show, on_tool, trace, record, t0):
    first, turn = None, _turn_ctx(msgs)
    for _ in range(ROUNDS):
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
        if record is not None:
            record.extend(_server_pairs(final.content, person, "voice"))   # her searches, as real calls in the history
        if final.stop_reason == "pause_turn":
            # a long web search paused on the server's side: the same turn goes
            # back as it is and the server goes on with it
            msgs.append({"role": "assistant", "content": final.content})
            continue
        if final.stop_reason != "tool_use":
            break
        results = _tool_round(final, msgs, person, on_show, trace, record, "voice")
        params = _escalate(final, params, results, turn)
        on_chunk(" ")                    # text before and after a tool call stays apart
    if final.stop_reason == "refusal":
        log("refusal:", getattr(final.stop_details, "category", None))
    return {"final": final, "cut": False, "first": first}


LAST_VOICE = [0.0]    # when the voice channel last called Claude (the keep-alive waits for a pause)
WARMED = [0.0]        # when a keep-alive last refreshed the cache (successfully)
WARM_AFTER = profile.tune("llm.warm_after", 50 * 60)      # s of silence before the cache is refreshed
WARM_HOURS = profile.tune("llm.warm_hours", (7, 23))      # only while the home is awake
CACHE_TTL = 60 * 60                                       # the 1h breakpoint keepalive() sets


def warm_due(now=None):
    """Is a keep-alive worth its money now? Only to extend a cache that is still
    alive and that a real voice turn of today made. Never the day's first paid call:
    a cold cache is written in full (~$0.28) for a room nobody talks in - the 07:00
    warm and the one 50 min after every brain restart, $0.56 of a day without a
    single conversation (2026-11-27)."""
    now = now or time.time()
    touched = max(LAST_VOICE[0], WARMED[0])
    if not LAST_VOICE[0] or time.strftime("%Y-%m-%d", time.localtime(LAST_VOICE[0])) \
            != time.strftime("%Y-%m-%d", time.localtime(now)):
        return False                     # nobody has asked her anything today
    h0, h1 = WARM_HOURS
    if not h0 <= time.localtime(now).tm_hour < h1 or ACTIVE[0]:
        return False
    quiet = now - touched
    return WARM_AFTER <= quiet < CACHE_TTL - 120     # a dead cache is not re-made: that is a cold write


def keepalive(system, history, person):
    """The voice conversation's cache kept warm through a pause: the same prompt, tools
    and history with max_tokens 0 - billed as a cache read only (~$0.007), no answer. A
    first request after an hour's pause wrote ~60k tokens again (~$0.25, 2026-11-26).
    The breakpoint is on her last words, not on the placeholder: the next real request
    reads up to there."""
    msgs = prepare(history, person)
    if not msgs or msgs[-1]["role"] != "assistant":
        return None
    last = msgs[-1]
    blocks = last["content"] if isinstance(last["content"], list) else [{"type": "text", "text": last["content"]}]
    blocks = [dict(b) for b in blocks]
    blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    msgs = msgs[:-1] + [{"role": "assistant", "content": blocks},
                        {"role": "user", "content": "(служебно: прогрев, ответа не нужно)"}]
    params = _params(system, person, 0)
    params.pop("cache_control", None)        # not keyed to the placeholder
    r = CLIENT.messages.create(messages=msgs, **params)
    usd = cost.add(params["model"], r.usage, "warm")
    log(f"cache warm: read {r.usage.cache_read_input_tokens} written {r.usage.cache_creation_input_tokens} ${usd:.4f}")
    if r.usage.cache_read_input_tokens:
        WARMED[0] = time.time()
    else:                                # nothing read: the cache had died - no more warms until she talks
        stop_warm()
    return r.usage


def stop_warm():
    WARMED[0] = LAST_VOICE[0] = min(LAST_VOICE[0], time.time() - CACHE_TTL)


ACTIVE = [0]          # turns being answered now: a deploy waits for 0 (brain /health "busy")
_ACTIVE_LOCK = threading.Lock()


@contextlib.contextmanager
def _turn():
    with _ACTIVE_LOCK:
        ACTIVE[0] += 1
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            ACTIVE[0] -= 1


def final_text(message):
    return "".join(b.text for b in message.content if b.type == "text").strip()


def complete(system, history, person, on_show=None, max_tokens=2048, trace=None, record=None):
    """The whole answer as text (text channels)."""
    msgs = prepare(history, person)
    params = _params(system, person, max_tokens)          # Sonnet; Opus only when there is data to weigh
    cost.check()                                          # at the start of a turn only (see stream)
    with _turn():
        return _complete(msgs, params, person, on_show, trace, record)


def _complete(msgs, params, person, on_show, trace, record):
    parts, turn = [], _turn_ctx(msgs)
    for _ in range(ROUNDS):
        r = CLIENT.messages.create(messages=msgs, **params)
        usd = cost.add(params["model"], r.usage, "chat")
        log(f"cost {params['model'].replace('claude-', '')} ${usd:.4f}: read {r.usage.cache_read_input_tokens} "
            f"written {r.usage.cache_creation_input_tokens} in {r.usage.input_tokens} out {r.usage.output_tokens}")
        if record is not None:
            record.extend(_server_pairs(r.content, person, "chat"))        # her searches, as real calls in the history
        part = final_text(r)
        if part:
            parts.append(part)
        if r.stop_reason == "pause_turn":            # a web search goes on (see stream)
            msgs.append({"role": "assistant", "content": r.content})
            continue
        if r.stop_reason != "tool_use":
            break
        results = _tool_round(r, msgs, person, on_show, trace, record, "chat")
        params = _escalate(r, params, results, turn)
    if record is not None:
        record.append({"role": "assistant", "content": final_text(r) or "…"})
    # only the last round's words: the text before a tool call is her "сейчас
    # посмотрю" or thinking aloud ("…так что сначала найду, а поздравлю в
    # ответе" came to Telegram, 2026-11-19); the voice speaks it, a chat does not
    return final_text(r) or "\n\n".join(parts)
