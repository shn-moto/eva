# What she did, kept whole: plan (2026-11-25)

## The problem, every time

She says something, a minute later she "confesses" it was a guess - though
she had looked. 2026-11-19 a search (a note in her own text); 2026-11-24 a tyre
size (a 200 px copy of the photo); 2026-11-25 06:59 the news (server search
blocks dropped); 2026-11-25 10:07 a week forecast: the page *was* opened, the
history kept its first 1500 characters (meta lines) and she concluded «ни одну
страницу с ним я тогда не открывала».

The cause is one: **the conversation history is her only memory of what she
did, and it is lossy on purpose** (results cut to KEEP_RESULT, pages to 1500
chars, pictures to 512 px, old turns trimmed - for price and size). The prompt
asks her to be honest about what she did; she checks herself against the cut
copy and finds "nothing". Each fix so far patched one kind of loss.

## The fix: an action journal, whole, outside the prompt

1. **Every action is kept whole** - each tool call (ours and Anthropic's server
   tools: search, page fetch, the filtering code) with its full input and its
   full result (text; a picture as its file), in Postgres `eva_actions`
   (id, ts, channel, person, reply, tool, input, result, source url / retrieved
   time). Written as the action happens, for voice and Telegram alike, for the
   API and (later) the Claude Code backend alike.
2. **The history keeps a short copy plus the address**: the cut result ends
   with `[сокращено; полностью - действие #1234]`, a picture copy likewise.
   The prompt stays as small as now.
3. **A tool `action_recall(id)`** (local, free, instant): the whole result back.
   She can re-read what she saw instead of fetching again or confessing.
4. **Each reply records what it stood on**: the reply's row lists the action
   ids its facts came from. Her reply in the history carries a service line
   «основано на действиях #1231, #1234» - not her words, a fact of the system.
5. **The rule in the prompt changes** from "be honest about what you did" to:
   «Сокращение в истории - не значит, что ты этого не видела. Прежде чем
   сказать, что ты что-то не проверяла или придумала, открой действие через
   action_recall. Ошибку признаёшь, только если полный результат её показывает.
   Если данные устарели (дата страницы), так и скажи - это не выдумка.»
6. **Freshness is part of the result**: a page's `retrieved_at` / date goes into
   the short copy too («страница от 22.10.2026») - the real error at 10:06 was
   a month-old page read as today's forecast.

## Why this and not a bigger history

A bigger history only moves the cliff (we went 24k -> 120k chars already); the
trim still lands somewhere, and whole pages/pictures in every request cost on
every phrase. The journal keeps the prompt small and makes the loss
recoverable: nothing she saw is gone, it is one cheap call away.

## Same journal, everything else gets simpler

- Short-term memory across channels (memory.recent) can point to actions too.
- The Claude Code backend (docs/BRAIN-PLAN.md) writes the same journal: one
  memory of deeds whatever thinks.
- The admin page can show, under a reply, what it was based on.

## Order

1. Table + writing every action (client and server tools) - nothing else changes.
2. History: short copy with the action address and the source date.
3. `action_recall` tool + the prompt rule; each reply's action list.
4. Check on the cases that failed: news (06:59), the week forecast (10:06), a
   photo read; ask her a minute later "ты правда это прочитала?".
