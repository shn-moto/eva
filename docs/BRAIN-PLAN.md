# Eva's brain, rebuilt: plan (2026-11-25)

Todo 1. Questions asked: should the brain live in its own container; local
classifier first; thinking either by the API (tokens, as now) or by the
subscription (through Claude Code); one persona whatever the mode; when one
mode fails (no money on the balance, limits) switch to the other; switching and
the first Claude Code login on the admin page; later a "coder" skill (Eva works
on the projects that run on shn-linux).

## Where we are

- The brain (`app/brain.py`, `dialog.py`, `chat.py`, `core/*`, skills) runs on
  the Station, next to the body (speaker, screen, LED ring, buttons, music).
- Every phrase: the hub's ear -> the Station's brain -> the local router on the
  hub's GPU (`core/router.py`, Qwen3-4B, ~0.2 s: ignore / ack / catalog commands)
  -> for "claude": one tool loop over the Anthropic API (`core/llm.py`): Sonnet
  low, which also weighs its tools' data; Opus medium only on a reason (Sonnet's
  think_deeper, a heavy tool, a correction, a long chain, errors, big data -
  `llm._escalate`, since 2026-11-25), Opus with a 5-min cache; prompt caching (1 h), server tools web search/fetch, daily budget (`core/cost.py`).
- The ear moved to the hub on 2026-11-24 (docs/EAR-PLAN.md): an utterance is
  made on the hub, travels to the Station, and the brain's calls go back to the
  hub (router, voices, memory, photolib, finances...). The brain is the last big
  thing on the Station that is not physical.

## Decision 1: the brain goes to the hub, in its own container

**The Station keeps the body only** (`eva-body`, a new small service):
play audio (her speech, music), the screen (ui_server + WPE), the LED ring, the
buttons, the volume, and the local flags the ear needs (`/run/eva/speaking`,
`music`, `hearing`) - set where the sound is actually played, so the echo logic
stays exact. A command API over the LAN (HTTP + one event stream back:
button pressed, playback started/ended, screen touched).

**The hub runs the brain** as a container `eva-brain` (deploy/linux.sh, like
the other services): dialog, chat (Telegram), skills, the LLM layer, TTS
synthesis (its audio streamed to the body), state files on a volume
(history, chats, panels, spend...). The ear's utterances reach it inside the
hub - no trip to the Station and back.

| | container on the hub (chosen) | systemd on the hub host | stays on the Station |
|---|---|---|---|
| deploy | same as every service: image per commit | venv + unit by hand | as now |
| speed | 16 cores, next to router/STT/DB | same | 4 weak cores, eMMC with 600 MB free |
| Claude Code | node + CLI in the image, login on a volume | on the host, mixed with the user's own | hardly fits (RAM, disk) |
| isolation | an LLM with tools sees only its volume and the services' APIs | sees the host user's files | - |
| coder skill | needs a deliberate door to the projects (see below) | has everything (too much) | no |
| hub down | Eva silent | same | Eva deaf anyway (STT is on the hub) |

Cost of the move: `dialog.py` is tied to the body in-process today (the speech
player and its "speaking" state, barge-in by «стоп», music ducking, the screen,
the slideshow). Those calls become body commands; the timing-critical parts
(stop the voice at once on «стоп», duck the music on her name) are done by the
body locally on the brain's command or on the ear's flag. This is the biggest
piece of work in the plan.

## Decision 2: thinking behind one interface, two backends

`core/think.py`: `Thinker.stream(system, history, tools, person, on_chunk,
on_tool, on_show)` / `.complete(...)` - what `llm.stream`/`llm.complete` do
now. The rest of the brain never knows which backend answered.

1. **Local router first** - as now (ignore / ack / catalog commands on the GPU,
   no paid call). Unchanged.
2. **ApiThinker** - today's `llm.py`: Messages API, our own history, our cache
   control, Sonnet -> Opus escalation, server web tools, the budget.
3. **CodeThinker** - Claude Code through the Agent SDK (Python
   `claude-agent-sdk`), signed in with the subscription:
   - the same system prompt **replacing** Claude Code's own
     (`system_prompt=`, not append) - no coding persona leaks in;
   - Eva's skills as an in-process SDK MCP server (`mcp__eva__*`), the same
     access checks per Person; Claude Code's own file/shell tools **off**
     (`allowed_tools` = Eva's tools + its WebSearch/WebFetch, which stand in for
     the API's server web tools); no CLAUDE.md, hooks or settings from disk;
   - one long-lived client per conversation (the voice, each Telegram chat) fed
     the new messages - the CLI starts once, not per phrase;
   - the same model choice (Sonnet for the first word, Opus to weigh data).

**One persona** comes from what both backends share: the same system prompt
(`core/prompt.py`), the same tools and rules, the same history (ours: when the
mode changes, the new backend starts from our recorded conversation, not from
nothing), the same short-term memory from the base (`memory.recent`). A test
set of ~20 typical requests is run on both before the switch is offered, and
the answers compared.

## Decision 3: modes and failover

Admin setting `brain.mode`: `api` | `subscription` | `auto` (default `auto`,
primary chosen on the page).

- An API answer "credit balance too low" / billing error, or our daily budget
  reached -> the subscription, until the balance works again (re-tried every
  30 min with a 1-token call).
- A subscription "usage limit reached" (it says when it resets) -> the API until
  that time.
- Both down -> she says so once («я сейчас не могу думать, лимиты»); the router's
  local commands still work.
- Every switch: a line in the log, a note on the admin page, a Telegram message
  to the admins.

## Admin page: "Мозг"

Mode (api / subscription / auto), which one answered last and why it switched,
API spend today (as on the TV), Claude Code: signed in or not, plan, the reset
time of a limit. **Login**: `claude setup-token` (a long-lived token for the
subscription, made once in a browser) - the page shows the link, takes the code
back and stores the token in the brain's secrets volume; "Выйти" removes it.

## Pros and cons of the subscription mode

**For**
- A flat price: the heavy agent rounds (Opus, many tools) cost nothing extra
  within the limits; today they are most of the API bill.
- Claude Code's world: its web tools, subagents, and later the coder skill.

**Against**
- **Limits are shared with your own Claude Code** - the 5-hour and weekly
  windows of the plan are one pool: Eva's day can eat your working quota, and a
  long work session can leave her on the API. This is the main cost.
- **Latency for the voice**: a turn through the CLI adds its own overhead; the
  first word must stay ~1 s. To measure before deciding (stage 2) - possibly
  the voice's first round stays on the API and the subscription takes the
  text channels and the heavy rounds.
- **Less control**: caching, effort, token use are Claude Code's; spend is
  reported, not steered.
- **Terms**: the plan is for one person's own use; Eva serving the household is
  personal use of your own account, but she must never be offered to anyone
  else on it.

**A third way, cheaper to build**: since 2026-10-07 Max/Ultra/Team plans include
monthly **API credits** for an API key of the linked Console organization. If
your plan has them, "pay by the subscription" is just another API key in the
same ApiThinker - no Claude Code, no latency question; Claude Code then stays
for what only it gives (the coder skill). Check this first.

## The coder skill (later, not in this plan)

Claude Code with file and shell tools on the projects of shn-linux is a
different trust level from a voice assistant. It gets its own runner, not the
brain's container: an explicit list of project folders, a separate login, only
for adults, every change on a branch, no deploy without a «да» in Telegram.

## Order

1. **Body / brain split** - `eva-body` on the Station, `eva-brain` container on
   the hub, the ear's utterances straight to it; behaviour unchanged (API only).
   Checked with the scenarios that work today (music, «стоп», photos, Telegram,
   printing).
2. **Thinker interface** + ApiThinker (a move of `llm.py`); CodeThinker as a
   prototype; measured on the same requests: first word, whole answer, tool
   use, persona.
3. **Modes, failover, admin page** (login, switch, status).
4. Coder skill - its own plan.

Sources: Claude Code headless/CLI and authentication docs (code.claude.com/docs),
"Use the Claude Agent SDK with your Claude plan" (support.claude.com, updates of
2026-06-15 and 2026-10-07).
