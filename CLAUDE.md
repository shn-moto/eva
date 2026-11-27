# Eva - working on this repo (bootstrap)

Read this first. The household-specific half (hosts, keys, paths on the hub, the DB, the
live state) is in **`CLAUDE.local.md`** next to it - not in git, read it too.

Eva is a home voice assistant. A rooted Yandex Station 1 on our own Debian is the **body**
(mics, speaker, HDMI screen, LED ring, keys); everything that decides or computes runs on
the **hub** (a Linux server with a GPU, docker): the ear, STT, voices, emotions, the brain
(dialog, Claude, skills, Telegram), the router, music, print, the admin page.

## The repo is PUBLIC (github.com/shn-moto/eva)

- Only universal code goes into git. Never commit family names, the address, health, money,
  secrets, voice recordings, device images. Comments and examples use made-up names
  (Иван, Маша, Лиза, Соня, Ольга).
- Household data lives in the private profile (`eva/`, gitignored; format in `eva.example/`):
  `profile.json` (household, speech, services, `tuning.*`), `BOOTSTRAP.md` / `LEGEND.md` /
  `PRIVATE.md` (persona), `names.json`. No hard-coding: a threshold or a name goes to the
  profile, read by `profile.tune(key, default)` / `profile.service(name)`; env `EVA_<KEY>` wins.
- Before every commit: `git diff --cached` scanned for real names and secrets.

## Conventions

- Code, comments, identifiers, logs, commit messages: **English**. Talk to the user: Russian.
- Commits: short message, author `shn-moto <shn-moto@users.noreply.github.com>` (repo-local
  config, already set), **no Co-Authored-By / AI trailer**. Work on `main`.
- Comments say *why* and carry the date and the incident that taught it
  (`(2026-11-26: the TV's cartoon got through)`) - keep that style, it is the project's memory.

## Standing permission: finish the job end to end

The user wants a change done, not discussed: when a task is finished and checked, **commit,
push to origin/main and deploy the affected services without asking** - that is routine here.
Still ask before: deleting data on the hub or the Station, schema changes to shared databases,
touching the other home projects' containers (photolib, finances, power, trading...), anything
that spends money (paid APIs beyond a normal test), and flashing / rebooting the Station's OS.

## Where things run

| part | source | runs as | deploy |
|---|---|---|---|
| brain (dialog, chat, skills, LLM, TTS, listeners) | `app/` (`brain.Dockerfile`) | `eva-brain` on the hub, host net, :8081 | `deploy/linux.sh brain` |
| ear + STT + voices + places | `services/stt` (`hear.py`, `ear_stream.py`, `voices.py`) | `newalisa-stt` :8901, raw mic stream :8912 | `deploy/linux.sh stt` |
| sound events + speech emotion (PANNs, GigaAM-Emo) | `services/voice-events` | `eva-voice-events` :8911 | `deploy/linux.sh voice-events` |
| admin page (journal, voices, places, calendar, listeners) | `services/admin` | `eva-admin` :8908 | `deploy/linux.sh admin` |
| music | `services/music` | `newalisa-music` :8904 | `deploy/linux.sh music` |
| print + office->PDF | `services/print` | `eva-print` :8909 (host net, CUPS) | `deploy/linux.sh print` |
| local router (Qwen3-4B, llama.cpp CUDA) | no source, a prebuilt image | `eva-router` :8906 | `deploy/linux.sh router` |
| GPU arbiter (router off while photolib indexes) | `services/gpu-arbiter` | `eva-gpu-arbiter` :8907 | `deploy/linux.sh gpu-arbiter` |
| body: raw capture, playback, screen, LED, keys | `app/ear.py`, `app/body.py`, `ui/spike/`, `os/rootfs/files` | systemd `eva-ear`, `eva-body` (:8085), `eva-ui`, `eva-ui-http` on the Station | `deploy/station.sh` |

The flow: Station `eva-ear` streams raw capture -> hub `ear_stream.py` (AEC3, Silero VAD, cut)
-> `hear.py` (STT, direction, who, splits, sound verdict; emotion 1-2 s later) -> event stream
`/hear/events` -> brain `core/hub_ear.py` -> `dialog.on_phrase` -> router -> local action or Claude.
Every logged phrase also goes to `core/listeners.py`.

## Deploy - only from git

```
git push origin main
deploy/linux.sh <service>        # git archive of origin/main -> image <name>:<sha> -> container recreated, health waited
deploy/station.sh --diff          # what would change on the Station
deploy/station.sh                 # app/ + ui/spike + rootfs files -> /opt/eva, affected units restarted
```

- The scripts refuse a commit that is not on origin. They read hosts and keys from
  `deploy/local.env` (gitignored; `local.env.example`). Run them from Git Bash in the repo root.
- **Never `docker cp`** code into a container or edit `/app` there: lost on the next recreate
  (the speaker split was, 2026-11-23).
- Deploy every service whose source changed; a change crossing services (a new event from the
  ear to the brain) needs all of them - **brain last**. `deploy/linux.sh brain` waits until she
  is not mid-answer (`/health` busy=false) - never bypass that.
- A deployed commit is recorded in `~/eva-src/<svc>/DEPLOYED` on the hub, `/opt/eva/DEPLOYED`
  on the Station; the image tag is the short sha (`docker ps` shows it).

After a deploy, check (commands in `CLAUDE.local.md`): the containers are up at the new sha;
the brain's log has `hub ear: connected` and no traceback; then the feature itself in the
service log or `eva_log`. Say plainly what could not be checked live (e.g. no speech yet).

## Checking without the hub

- Python: `python -m py_compile <files>`; logic of a module: a scratch script that imports it
  from `app/` with stubbed side effects (`core.listeners` with `_act` replaced worked well).
- Admin page: open `services/admin/index.html` in the browser pane and drive it with JS on
  stubbed data (no backend there; `api()` calls fail, set `S.rows` / `LS.items` and render).
- Logs of everything (Station units and hub containers): table `eva_log` in Postgres
  `assistant_memory` (`unit`, `ts`, `msg`). Listener firings are lines `listener <id>: ...`.

## Rules the user set (firm - do not "improve" around them)

- **Station = hardware only.** No DSP there at all (not even AEC/VAD): raw samples go to the
  hub. Never stop a move halfway; after one, audit the Station for anything non-physical.
- **Reaction time is top priority** (phrase end -> first sound, logged as `latency:`).
- **Over music only her name interrupts**: no unnamed commands, no router exception, no ducking.
- **Model tiers: router -> Sonnet -> Opus.** Every new tool gets a tier in the same change: a
  router intent (`core/router.py` INTENTS + examples) or `LIGHT_TOOLS`; heavy only if its result
  needs weighing. Printing never reaches Opus. A prompt change costs a cache write - batch them.
- **Actions need evidence in history**: any path that acts without Claude (router, scheduler,
  listener, relay) writes a real tool_use + tool_result into `dialog.history`, else she redoes
  it (double print, 2026-11-26). Side-effect tools get an idempotency guard.
- **Tools are generic, not scripted**: give read access (SQL read-only role, the whole API)
  and let the model reason; writes to the smart home are forbidden.
- **Admin corrections are enrollment**: a speaker fixed on the admin page trains the voice
  base automatically; never ask a child to enroll by voice.
- Before writing audio/ML code ourselves, look for a ready-made part first.

## Docs

`docs/ARCHITECTURE.md` (core + skills + presentations), `docs/BRAIN-PLAN.md` (brain on the hub),
`docs/EAR-PLAN.md`, `docs/EAR-AUDIT-2026-11-26.md`, `docs/EMOTION-PLAN.md`, `docs/BACKLOG.md`,
`todo.txt` (the user's own list, Russian). `os/README.md`: the Station's boot chain and
partitions. `README.md`: overview and how the Station was rooted. Each skill: `app/skills/<name>/`.
Module docstrings are the reference for their file formats (e.g. `core/listeners.py`).
