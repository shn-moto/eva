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

## Open questions

- Which children's states matter most: crying, screaming, fighting, fear? Should a child's "angry" speech without crying trigger too?
- Should she step in at once, or first ask a parent in Telegram?
- Who gets the Telegram alerts: always the admin, or whoever is not at home?
- Keep the clips of events (for review) or only the log line?
