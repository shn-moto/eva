# Emotions and sound events in the voice: plan

Goal: hear not only words but how they are said - crying, screaming,
shouting, anger, fear, laughter. Log every such event and use the log as
triggers for scenarios:
- a child cries or screams - Eva steps in: tries to understand what happens and helps calm down;
- adults shout - log only;
- an unknown adult shouts - an event for the cameras (record), once vision exists (todo 2).

Speech is Russian.

## Two different problems

1. **Sound events that are not words**: crying, sobbing, screaming,
   shouting, laughter, a slammed door, breaking glass. This does not depend on
   the language. Models trained on AudioSet (527 classes, among them "Crying,
   sobbing", "Baby cry", "Screaming", "Shout", "Yell", "Children shouting",
   "Laughter", "Whimper").
   - Candidates: PANNs CNN14, BEATs, EfficientAT.
2. **Emotion of speech**: the same words said calmly or in anger. This does
   depend on the language and the culture, so the models must be trained on
   Russian.
   - Candidates:
     - **GigaAM-Emo** (Sber, trained on Dusha, about 300 h of Russian speech: angry / sad / neutral / positive). Same family as the STT already running.
     - Aniemore wavlm-emotion-russian-resd (RESD dataset: anger, disgust, enthusiasm, fear, happiness, neutral, sadness).
   - Text tone: the local Qwen on the transcript («отдай!», «мне больно», «перестань») - free, a second opinion.

Plus **who** it is:
- family voices are known (voice profiles); children are listed in the profile (household.children);
- for an unknown voice: child or adult, from an age / gender model (audeering wav2vec2-large-robust-24-ft-age-gender gives a "child" class) and pitch.

## Where it runs

- The hub, **on the CPU**: the GPU is full (7.8 of 8 GB: router 3.2 GB, photolib 3.9 GB). These models are small (100-300 M parameters); the 16 cores handle a phrase in ~0.1-0.4 s. Same container as STT, so the audio is already there.
- **Phrases.** Every phrase the ear already sends to STT gets, in parallel:
  - event classes;
  - speech emotion;
  - child / adult.

  The answer to the ear gains `emotion` / `events` fields, next to `doa`.
- **Loud sounds that are not phrases.** Crying and screams often do not pass the speech VAD. The ear gets a second, cheap trigger: loudness well above the room's level for > 0.5 s, not her own voice, not music. Then it sends that clip (with 1 s before) to `/events` on the hub.
- **Not on the Station**: its CPU is busy with the ear.

## The log

Postgres on the hub, table `voice_event`:
- ts, kind (cry / scream / shout / laugh / anger / fear / ...), confidence;
- who (voice label or "child?" / "adult?"), direction fingerprint;
- text of the phrase (if any), the clip's file on the hub.

Retention: 30 days by default, in the profile - this is private data. Clips sit next to the STT log (`~/eva-data`), never on the Station.

A view of it: the stats panel / a page on the TV on request, and Telegram to the admin on request («что было днём?»).

## Scenarios (triggers)

A small rule engine; the rules are in the private profile, not in code:

| When | Then |
|---|---|
| a child (known or "child?") cries / screams **≥ 8 s within 30 s**, no adult calm voice in between | Eva steps in: softly, by name if known; asks what happened, offers help (a song, a story, «позвать маму?»). Claude gets the last minute's transcript and events. |
| it goes on for N minutes after her try | Telegram to the parents (who is at home: who_heard) |
| an adult shouts / speaks in anger | log only |
| an unknown adult (voice not in the family) shouts | event "camera record" - logged now, wired to vision later (todo 2) |
| laughter, joy | log only (later: «весёлый день» for the slideshow) |

Guards:
- **No triggers from:**
  - her own voice;
  - music and the TV: the playback flags, plus the TV's direction learned from doa;
  - cartoons with crying: also need repetition, a known child's voice or the child class, and no music / playback.
- **Cooldown**: she steps in at most once per 10 minutes for the same child.
- **Manners**: no step-in at night (quiet hours from the profile), and «Ева, не вмешивайся» turns it off for an hour.
- **What she does not do**: no diagnosis, no scolding, no taking sides in a quarrel - only calm attention and help.

## Stages

1. **Measure (1-2 days).** Run the candidate models on our own audio: the STT log on the hub (14 days of real phrases) plus test clips (crying / screaming from AudioSet, cartoons, music, TV).
   - Pick the models by accuracy on our room and CPU time.
   - Russian speech emotion is the doubtful part; sound events usually work out of the box.
2. **Log only (a week).** Events are classified and written to `voice_event`, no actions. We look at what was caught: false alarms from the TV, cartoons, the canary; thresholds are tuned on real data.
3. **Triggers.** The rule engine with the tuned thresholds:
   - child crying -> she steps in;
   - adults shouting -> log;
   - unknown adult -> camera event (stub).
4. **Vision** (todo 2): the camera event becomes recording; video confirms who is in the room.

## Stage 1, first measurement (2026-11-23)

Tool: `tools/emotion-bench` (Docker, CPU only, 4 threads, one model per process).
Data: one day of the STT log, 248 phrases, 507 s, calm everyday speech: no crying or screaming in it.

| Model | Params | Peak RAM | s / phrase | p95 | RTF | Non-neutral on calm speech |
|---|---|---|---|---|---|---|
| GigaAM-Emo (Dusha, 4 classes) | ~240M | 2.4 GB | 0.64 | 0.94 | 0.31 | 27% (34 positive, 21 sad, 13 angry) |
| emotion2vec_plus_base (9 classes) | ~90M | 1.9 GB | 0.42 | 0.70 | 0.21 | 31% (mostly surprised / sad) |
| audeering msp-dim (arousal / valence) | ~300M | 1.4 GB | 0.59 | 1.04 | 0.29 | arousal p50 0.32, p95 0.55, max 0.79 |
| AST AudioSet (527 classes) | ~87M | 0.8 GB | 3.62 | 3.69 | 1.77 | top-1 Speech 232/248; crying, screaming, shout, laughter all < 0.06 |

What it says:
- **Speed is fine** for the speech-emotion models (~0.5 s per phrase on 4 cores). **AST is too slow**: it pads every clip to 10 s, 3.6 s per phrase whatever its length. For sound events take a CNN instead (PANNs CNN14 or EfficientAT) and measure it.
- **Memory is the limit**: the hub has ~5 GB free, so one speech-emotion model plus a small event model, not two speech-emotion models. An int8 / ONNX export can shrink them later.
- **One phrase alone is not a signal**:
  - Short phrases (one word, < 1.5 s) get confident nonsense: a name called softly scored "positive 1.00", "так" scored "angry 0.98".
  - The two classifiers agree on the label rarely.
  - The clear cases do agree: a sad rhyme read aloud was sad in both, laughter was positive / happy in both, a sharp refusal was angry in both with high arousal.
  - So: only phrases ≥ 2 s, and repetition over time (the scenario rules already ask for ≥ 8 s within 30 s).
- **AST missed the laughter phrase** (laughter 0.01): the phrase clips are cut by the speech VAD and high-passed, so non-speech sounds need their own trigger (the loudness trigger in "Where it runs"), not the phrase stream.
- **Not measured yet: the positive cases.** Real crying, screaming and shouting, a child's voice in anger. They need clips: staged recordings in the room, cartoons on the TV, public sets (ESC-50 has crying baby / laughing).

Leaning: GigaAM-Emo for speech emotion (Russian, made sense on the clear cases), audeering arousal as the second axis if memory allows, a CNN AudioSet model for events. Decide after the positive clips.

## Stage 1, second measurement: ESC-50 (2026-11-23)

Data: 15 classes of ESC-50 (CC BY-NC), 40 clips × 5 s each = 600 clips:
- crying_baby, laughing;
- 13 sounds of a home: breathing, coughing, sneezing, snoring, cat, dog, knock, footsteps, clapping, keyboard, vacuum, glass breaking, siren.

Plus PANNs CNN14 (AudioSet, 80M) on the STT log.

**PANNs CNN14** - clips with a crying / laughter / scream score above the threshold:

| Class | cry > 0.3 | cry > 0.1 | laugh > 0.3 | scream > 0.3 |
|---|---|---|---|---|
| crying_baby (40) | **30** | **38** | 3 | 1 |
| laughing (40) | 1 | 1 | **12** | 2 |
| 13 other classes (520) | 0 | 0 | 0 | 0 |
| our speech, STT log (248) | 0 | 0 | 0 | 0 |

- No false alarm at all: not on the home sounds, not on our speech (the highest cry score on speech: 0).
- Laughter is weaker (12/40), the threshold for it should be lower and backed by repetition.
- 0.16 s per phrase (RTF 0.08), 1.2 GB, 0.26 s per 5 s clip. 23x faster than AST.

**The speech-emotion models on non-speech sounds:**
- GigaAM-Emo: crying baby = "positive" 33/40, dog and glass = "angry", cough = "positive". Meaningless.
- emotion2vec: closer on voice sounds (crying = sad 32/40, laughing = happy 40/40), but random on the rest (vacuum = sad, siren = fearful).
- audeering arousal: high for every non-speech sound (0.6-0.84 median against 0.32 for our speech), so it cannot tell crying from a vacuum cleaner.

**Decision for stage 2 (log only):**
1. **PANNs CNN14 on everything**: every phrase plus the clips from the loudness trigger. It decides what the sound is: speech, crying, laughter, scream, music, TV.
2. **Speech emotion only where PANNs says speech** and the phrase is ≥ 2 s. GigaAM-Emo (Russian) as the main one; emotion2vec stays the alternative (lighter, better on voice sounds, weaker on Russian - to compare on the week's log).
3. Memory: PANNs 1.2 GB + GigaAM-Emo 2.4 GB ≈ 3.6 GB of ~5 GB free on the hub. Separate container `voice-events` on the CPU.

**Still not measured:**
- screaming and shouting (ESC-50 has none);
- the crying of an older child (ESC-50 is infants);
- cartoons and the TV as a source of false alarms.

The week of logging (stage 2) answers these on real data; staged clips can speed it up.

## Stage 2 started (2026-11-26)

- `services/voice-events` (container `eva-voice-events`, CPU, ~2.5 GB): PANNs CNN14 `/classify` (~0.2 s,
  in parallel with STT, the ear waits for it) and GigaAM-Emo `/emotion` (~1-2 s, asked after the phrase
  went on, never waited for). Checkpoints from `~/eva-data/bench-cache`.
- Every phrase gets `sound` {voice, kinds (cry / laugh / scream / shout / singing / music / bird / tv),
  verdict, top} in its clip json and in the utterance; speech emotion for a voice of >= 2 s.
- **Noise**: voice < 0.15 = not a voice (the STT made words of a car, a door, music): on 2181 phrases it
  takes 86 of 367 short ones and 2 of 711 family phrases. The brain drops such a phrase (no listeners,
  no dialog) unless her name is in it; the admin page shows «шум (<class>)».
- Logged only: `hear: sound: <verdict> ...` for non-speech / cry / scream / shout / laugh >= 0.3 and
  `hear: emotion: <label> <score>` for non-neutral >= 0.7. No table `voice_event` yet, no actions.
- Not done yet: the loudness trigger for sounds that do not pass the speech VAD (crying, screams),
  the table, the week of review, the rules.

## Open questions

- Which children's states matter most: crying, screaming, fighting, fear? Should a child's "angry" speech without crying trigger too?
- Should she step in at once, or first ask a parent in Telegram?
- Who gets the Telegram alerts: always the admin, or whoever is not at home?
- Keep the clips of events (for review) or only the log line?
