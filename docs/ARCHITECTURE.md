# Eva — architecture (v2, skills and screen)

Status: step 1 done 2026-11-19 (core + skills registry in app/core, app/skills; memory and family as skills; dialog.py / chat.py / thin brain.py). Steps 2-3 done, 4 open. Replaces the single `brain.py` that grew while we
were bringing voice, memory, voices, Telegram and photolib up one by one.

## The idea in one picture

```
            channels (in/out)                     core                          skills
 ┌──────────────────────────────┐   ┌────────────────────────────┐   ┌──────────────────────────┐
 │ voice   ear.py ─▶ phrases     │──▶│ identity: who is it?       │   │ memory    (assistant_mem) │
 │         ◀─ speech + lips      │   │   voice profile / telegram │   │ family    (photolib tree) │
 │ screen  ui_server ─▶ TV       │◀──│   ─▶ one Person (photolib) │──▶│ photos    (photolib media)│
 │ telegram telegram.py          │   │ conversation per person    │   │ finance   (home-finances) │
 │         ◀─ text, photos       │   │ llm: Claude + tool loop    │   │ system    (volume, reboot)│
 └──────────────────────────────┘   │ policy: who may use what   │   │ …                         │
                                     └────────────────────────────┘   └──────────────────────────┘
```

- **Channels** carry words in and results out. They know nothing about photos or
  money: a tool result says *what* to present, the channel decides *how*
  (TV screen for the voice channel, a photo album message for Telegram).
- **Core** is small and stable: who is speaking, what was said before, one
  Claude tool loop (streamed for voice, whole for text), and the access policy.
- **Skills** are where the abilities live. Adding one never touches the core.

## Identity: one person everywhere

photolib is the source of truth for people (decided with Иван, 2026-11-19).

| Signal | Resolves to |
|---|---|
| voice profile (CAM++) | `voices.PERSON[profile]` → photolib `person_id` |
| Telegram account | photolib `app_user.telegram_id` → `person_id` |
| unknown voice | guest (cluster `гость-N`), no `person_id` |

`Person = {person_id, name, is_admin, permissions (photolib sections), channel}`.
New family member: add them in photolib (user + person); introduce the voice
once («Ева, я Лиза, запомни мой голос») and the profile is linked to that person.

## Skills

```
app/skills/<name>/
    __init__.py     SKILL = Skill(name, summary, tools=[...], prompt=..., context=...)
    SKILL.md        what Eva knows about this area (loaded into the prompt only when needed)
```

A skill declares:

- **summary** — one line for `skills/INDEX.md`-style listing in the prompt;
- **context()** — optional text always in the prompt (e.g. family: the household
  and today's birthdays); kept short, refreshed on a timer;
- **tools** — each `Tool(name, description, schema, handler, needs=...)`:
  - `needs`: the access it requires — a photolib section (`search`, `persons`,
    `timeline`, `map`…), `adult`, `admin`, or `known` (any recognised person);
    the core checks it against the Person before running the handler, so a
    skill never has to;
  - `handler(args, ctx) -> Result(text, show=None)`: `text` goes back to Claude;
    `show` is an optional **presentation** (below) for the channel.

Progressive disclosure: tool descriptions are short; a skill's `SKILL.md` is
loaded into the conversation by a `skill_help(name)` tool the first time it is
needed, not on every request (prompt stays small, cache stays warm).

## Presentations (what the screen and Telegram show)

A tool returns data, not UI. The same result works on any channel:

| `show` | TV (voice channel) | Telegram |
|---|---|---|
| `photos(ids, title)` | grid → tap/voice to open, slideshow | media group (up to 10) + "ещё…" |
| `photo(id)` | full screen, faces and place as captions | one photo with caption |
| `feed(from, to)` | chronological feed, scrolling | first N as album + link to the web feed |
| `person(id)` | card: portrait, dates, relatives, photo strip | text card + portrait |
| `tree(id)` | family tree around the person | link to photolib's tree page |
| `map(bbox)` | map with photo clusters | location + link |
| `page(url)` | full screen web page (finance portal…) | link |

The TV keeps Eva's face in a corner while something is shown and returns to
the face after a timeout or «Ева, закрой». Navigation is a core tool
(`screen(action: next|prev|open N|close|zoom)`) so «дальше», «открой третью»
work for every presentation.

Images reach the TV through the local UI server, which proxies and caches
photolib's `/image/{id}/thumb|full` (no cross-origin issues, works if the TV
page reloads, thumbnails stay on the Station's disk).

## photolib skills (first)

| Skill | Tools (needs) | API |
|---|---|---|
| **family** | `family_lookup(name)` (persons), `person_card(name)` (persons) | `/persons/family-tree`, `/persons/{id}` |
| **photos** | `photo_search(query, person?, period?, place?)` (search) | `/search/person-clip`, `/search/text`, `/ai/search-assistant` |
| | `person_photos(name, period?)` (persons) | `/persons/{id}/photos` |
| | `photo_info(id)` — when, where, who (photos) | `/photo/{id}`, `/photo/{id}/faces` |
| | `show_feed(period)` (timeline) | `/timeline/photos` |
| | `similar_photos(id)` (search) | `/search/similar/{id}` |
| later | albums, map, books, tags | `/albums*`, `/map/*`, `/books/list` |

Write operations of photolib (tagging, assigning faces, editing the tree) are
**not** exposed to Eva until we decide so explicitly.

## The other home projects (all on shn-linux, all over HTTP)

| Skill | Project | Read | Act |
|---|---|---|---|
| **photos / family** | photolib (Smart Photo Indexing, :8000) | above | none for now |
| **finance** | `D:\PRG\Pyton\home-finances` (:8686, Postgres `home_finances`) | spending by category / person / period, compare months (`/api/breakdown`, `/api/compare`, `/api/transactions`, or a read-only SQL role); `page` → the portal on the TV | none |
| **power** | `D:\PRG\Pyton\Home Power Meter` (:8484, TimescaleDB) | power now per socket and for the flat (`/api/live-summary`, `/api/devices/{id}/live`), energy per hour/day/month (`/api/summary`, `/stats`), sensors (`/api/sensors/summary`), meter readings | switch sockets/relays (`/api/devices/{id}/functions/{code}`), run automations (`/api/automations/{slug}/run`) |

**Acting in the physical world** (switching a socket, running an automation)
is a different kind of tool from reading: `needs=adult`, and the core asks
for a spoken confirmation before the handler runs («Выключить обогреватель в
детской? — Да»). Never from an unknown voice, never from children.

Each project keeps its own API and database; Eva only adds a skill module
that talks to it. A project's own `CLAUDE.md` / README stays the place to
learn it; the skill's `SKILL.md` is a short digest for Eva.

## Access policy

- photolib permissions decide photolib skills (the same rights as in its web UI).
- Unknown voice in the house: may chat and see the face screen; no personal
  data (photos, family details, finances, memory search) until recognised.
- Children: photos and family yes; finances no (BOOTSTRAP «Доступ»).
- `admin` (Иван): system skill (volume, reboot, updates).

## Process layout (unchanged where it works)

| Process | Role |
|---|---|
| `eva-ear` | microphones → phrases (+ voice embedding) |
| `eva-brain` | core + skills + voice output |
| `eva-telegram` | Telegram channel |
| `eva-ui-http` + `eva-ui` | screen: UI server (events, image proxy) + WPE |

Skills call their backends over HTTP (photolib, assistant_memory search,
home-finances), so moving the brain to shn-linux later is a deployment change,
not a rewrite.

## Order of work

1. Refactor: core (identity, conversation, llm loop, policy) + skills
   registry; move memory, family, voices, system into skills. Behaviour
   unchanged, checked by voice and Telegram.
2. Screen v2: presentation events, image proxy, grid/photo/feed views with the
   face in a corner, `screen` navigation tool. **Done 2026-11-19**: core/screen.py
   (state, pages of 11 - the 12th place is the face, idle close 3 min),
   skills/screen (`screen` tool, voice only), family `person_card`, ui_server
   /img/{id}/thumb|full + /face/{id} (thumbs on disk 60 MB, full in memory),
   views photos / photo / person in ascii.html. Feed view comes with step 3.
3. photos skill on photolib + Telegram presentations (media groups). **Done 2026-11-19**: skills/photos
   `photo_search` the way photolib's own search page does it (its
   /ai/search-assistant rules): Eva writes the English CLIP prompt (only what the
   camera sees, no names/places), multi_model (RRF), people, dates, and a place
   (`place` + `radius_km` -> /geo/geocode -> bbox); `similar_photos` (n on the
   screen); `show_feed`
   (timeline spread over the period, bursts thinned), `photo_info` (n on the
   screen: date, people, coordinates - no geocoder, she names the place);
   telegram.py sends presentations as albums (multipart upload, 10 per group).
   Rights are photolib's names: search.text, photos.view, persons.view.
4. power (reading first, then switching with confirmation) and finance as skills;
   person card, tree, map.

## Eva as an agent (2026-11-20)

She plans with her tools herself (prompt section AGENT: think what is needed,
act step by step, look before asking, contact others when that is the point of
the request, say the result). Up to 8 tool rounds per answer; text channels run
at effort medium, the voice at low. Tools added for this:

| Tool (skill) | needs | What |
|---|---|---|
| `who_heard` (home) | adult | when / what a family member last said at home (transcript, known voices only) |
| `say_at_home` (home) | adult | speak aloud through the Station; goes into the voice history, the dialog window opens for any answer |
| `send_message` (home) | adult | Telegram message to a family member; goes into their chat history with Eva |
| `document_search` (photos) | private (voice only) | hidden photographed documents (tags document / документы); no OCR - she reads them with `photo_look` |

Cameras: none yet - the prompt says so; a camera skill (look, find a person,
speak through it) slots in as one more skill.

## Data and charts (2026-11-20)

Step 4, reading part. Both apps already compute their numbers, so Eva reads
their HTTP APIs on the LAN (open there, no login), never their tables:

| Tool (skill) | needs | Source |
|---|---|---|
| `power_devices`, `power_usage` (power) | adult | Home Power Meter :8484 `/api/summary`, `/api/devices/{id}/stats?period=custom&start&end` (hour/day/month buckets by range) |
| `finance_breakdown`, `finance_series`, `finance_transactions` (finance) | private (voice only) | home-finances :8686 `/api/breakdown`, `/api/transactions`; the month loop runs in the tool |
| `show_html` (screen) | screen | her own HTML page in a sandboxed iframe: window (~880x560, face column on the right) or fullscreen (1280x720, no face); Chart.js 4 at `/lib/chart.umd.min.js` on the Station |

Switching sockets / running automations stays out (needs a confirmation flow).
