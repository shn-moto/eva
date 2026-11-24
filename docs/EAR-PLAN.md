# The ear on the server: plan (2026-11-24)

Goal: the Station only hears - microphones, echo cancelling, voice activity,
fragments cut by pauses and length. Everything that decides something about
the sound - text, direction, who speaks, where one person ends and another
begins, which fragments are one sentence, what is Eva's own echo, voice
learning - runs on shn-linux, in one place, next to the voice base and the
admin page. Faster (16 cores against 4 weak ones) and one source of truth.

This is the ear part of the 2026-11-18 architecture (Station = thin terminal,
eva-hub on shn-linux). When recognition moved to the hub, the voice decisions
should have moved with it; they did not, and the split decision ended up torn
between two machines.

## Why now: the bug that showed it

A voice cut found by the hub was believed by the Station only if both parts
were confidently known voices (`ear.split_ok`/`who`: best >= 0.5, lead 0.1),
against profiles the hub could not see. On the bench (glued phrases of three
family members + 46 single-speaker phrases) the hub found the change in 32 of
36 glues, the Station believed 18. Short parts from another room rarely reach
0.5. A rule that compares the parts with each other ("each part prefers its
own voice over the other's by 0.05") keeps 23-24 of 36 with no extra false
cuts (2 of 46, as now) - but it needs the profiles where the cut is searched.

Open hard case kept in the bench: a phrase from another room, one adult then
another, a child laughing over the second. Not solved by any rule above.

## What stays on the Station (physical, needs the samples in real time)

1. **Capture**: 7 mics + the amplifier reference (arecord, 16 kHz).
2. **Echo canceller**: needs the reference sample-aligned with the mics; the
   network would break that. Its measurement per fragment (share under her
   voice = `playback`, energy left = `residual`) goes as metadata; the hub
   decides what is echo.
3. **VAD** (Silero, 32 ms frames) and the **cut**, by two rules only:
   - **pause**: a fragment ends after `END_SILENCE` (0.8 s) of no speech;
     `END_SILENCE_SHORT` (1.1 s) when only a word was said (a lone «Ева, ...»).
     Pre-roll 0.64 s before, 0.3 s of tail after - as now.
   - **length**: a fragment never grows past `MAX_SPEECH`. Not a hard cut in a
     word: after `SOFT_MAX` (12 s) it ends at the first short dip of the VAD
     (>= 0.15 s, the gap between words); at `MAX_SPEECH` (20 s) it is cut
     anyway with a 0.6 s overlap into the next fragment. While her voice plays
     the allowed length is `PLAYBACK_WINDOW` (1.5 s, overlap 0.6 s) - the same
     rule with another number, so «стоп» over her voice is still heard.
   Every fragment carries its timeline: `seq`, `start`, `end`, `gap` (seconds
   since the previous fragment ended), `chained` (true when it was cut by
   length, not by a pause: the speech goes on).
4. **Sending**: each fragment = mono (canceller output) + the 7 raw mics +
   metadata, one POST to the hub. If the hub does not answer, the fragment is
   kept and sent again (as the STT retry now, up to ~25 s).
5. **Screen / LED**: "I hear you" at the start of speech is local (instant);
   everything after it ("held", the recognised text) comes from the hub.
6. **Results**: the ear keeps one stream open to the hub (SSE, started by the
   Station) and hands every finished utterance to the brain as now
   (`POST 127.0.0.1:8081/utterance`), with the speaker already named.

Removed from the Station: CAM++ (voice prints), `voice_sims`/`who`/`split_ok`/
`accepted_parts`, the Joiner (open ends, same voice/place glue), the echo
verdict, `voices.py` (identify, auto-learning, guest clusters, enrollment
state), the voice files in `/var/lib/eva/voices`.

## What the hub does with a fragment (one service: `eva-ear-hub`, in the STT container)

`POST /fragment` (mono + mics + metadata) -> 202; results go out on
`GET /events` (SSE). Per fragment, in a thread pool:

1. **Recognise** the whole fragment once (GigaAM RNNT, letters with times).
2. **Direction** from the 7 mics: per block, per word.
3. **Split** inside the fragment, up to 3 parts: by direction (a change of
   place), by voice (a change of voice in one place) - the cut search as now,
   but searched **inside the words only** (no leading/trailing silence: the
   hard case's cut went into 1.9 s of silence before the first word), and the cut
   **believed here**, with the profiles: the parts' best known voices differ and
   each part prefers its own (the rule measured above). One decision, one place.
4. **Who** each part is: voice print of the part (speech only) against the
   voice base; family name, guest cluster or unknown; Eva's own voice (her
   profile moves here too) -> echo verdict together with `playback`/`residual`.
5. **Sentence**: the Joiner, on the hub's timeline of fragments. A part whose
   text ends open («Ева», «какая погода в») or a `chained` fragment waits for
   the next fragment: glued when it is the same voice from the same place and
   the gap is short, else sent alone (same rules and numbers as the ear's
   Joiner now). The hub sees both fragments' audio, so the glued phrase is
   recognised again as one sound.
6. **Out**: each utterance = text, raw text, speaker (name, similarity, known),
   direction, `audio` (the clip in the STT log + span), `playback`, flags
   (echo, window, joined, split). Written to the STT log as now.

## Voices: one base, in Postgres (`assistant_memory`)

- `voice_prints (id, speaker, emb real[192], source, utterance_id, audio, created_at)`
  `source` = `enroll` (запомни мой голос), `correction` (admin page), `auto`
  (learnt from a sure long phrase), `cluster` (merged guest). Profile = all
  rows of a speaker; `auto` rows capped per speaker (newest 30) as now.
- `voice_guests (name, embs, count, first, last)` - guest clusters.
- Eva's own voice: `speaker = 'Ева'`, `source = 'self'`.
- Migration: `speakers.json`, `speakers_auto.json`, `speaker_clusters.json`,
  `eva-self.json` from the Station into the tables, once.
- **Admin corrections** go into the base directly: the admin page writes a
  `correction` print when a speaker is changed (it asks the hub `/emb` for the
  clip's print) - `voices.corrections_sync` on the Station goes away.
- **Enrollment** («Ева, запомни мой голос, я Маша»): the brain decides it is
  an introduction (words, dialog) and calls the hub: `POST /voices/enroll
  {name, utterance}` starts it, the next phrases of that voice are collected on
  the hub, `/voices/enroll/finish` ends it; the same `_enroll` code with the
  closer-than-others rule (9958f48). `claim` (is this voice plausibly X)
  becomes `GET /voices/claim`.

## The brain (stays on the Station for now)

Receives utterances with the speaker already decided: `item["speaker"] =
{label, sim, known}` instead of `item["voice"]` + `VOICES.identify`. Its three
`identify` calls (dialog.py 411, 503, 717) read the field; `introduction`
calls the hub. Moving the brain itself to the hub is the next step of the same
plan, not part of this one.

## Order of work

1. **Hub: voice base** - tables, migration, `/voices/*` (identify, claim,
   enroll, corrections), the admin page writes corrections. Station unchanged,
   the brain asks the hub instead of its own files (one switch).
2. **Hub: `/fragment` + `/events`** - the current pipeline (STT, direction,
   split, believed cut, who, echo, Joiner) on the hub. Checked **before the
   Station uses it**: a replay tool posts the STT log's fragments (mono +
   `-mics.wav` of the last 7 days) and compares with what happened; the split
   bench runs against `/fragment`.
3. **Station: the thin ear** - capture, canceller, VAD, pause/length cut,
   POST, SSE -> brain. CAM++ and the Joiner removed.
4. **Cleanup**: voice files and code off the Station, docs.

Each step deployed from git; the Station ear switch only when no music plays
and nobody is recording.

## Numbers that carry over (tuned, not to be lost)

`END_SILENCE` 0.8, `END_SILENCE_SHORT` 1.1 / `SHORT_HEAD` 0.9, `TAIL_KEEP`
0.3, `PREROLL` 20 chunks, `MIN_SPEECH` 0.35, `PLAYBACK_WINDOW` 1.5 /
`OVERLAP` 0.6 s, echo `playback > 0.5 and residual < 0.1`, Joiner `JOIN_GAP`
1.0 / `REST_WAIT` 12 / same voice 0.5 / places `AZ_OTHER` 30, `AZ_SAME` 12,
split `DIR_CUT` 0.05, `SNAP` 1.2, voice thresholds in `voices.py`.

## Network

Speech only (VAD on): mono 32 KB/s + 7 mics 224 KB/s while someone talks,
nothing in silence. The house LAN carries it easily; the 7 mics are already
sent now (`/doa`).

## Risks

- Hub down = Eva deaf. Already true today (STT is on the hub); fragments are
  retried for ~25 s, the screen shows it.
- Latency: one round trip less than now (/doa + /stt become one POST), the
  voice print no longer computed on the Station's slow CPU (it was ~0.3-0.5 s).
- The Joiner's timing: now it waits on the Station's clock; on the hub it uses
  the fragments' own timestamps (the Station's clock), so network delay does
  not change what is glued.
