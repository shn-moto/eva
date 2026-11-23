# Eva rework plan (2026-11-23)

Goal: Claude only for what needs thinking; recognition that hears names and whole phrases;
speakers told apart by direction. Measured state first, then stages in order.

## Measured on 2026-11-23

**Claude cost after the 2026-11-21 fixes.** 22.11: $0.30 a day, 23.11 so far $0.14.
- A warm request costs $0.003-0.008.
- A cold one (the first in an hour) costs $0.07-0.12: 18-28k tokens are written to the cache, at 2x the input price with the 1h TTL. Examples: avatar 11 c, children's songs 12 c, the garbage phrase «ева станет там» 7 c.
- Every request carries the same prompt:

  | Part | Tokens |
  |---|---|
  | persona + rules | 5.7k |
  | 8 skill prompts | 4k |
  | 25 tool specs | 5.3k |
  | history | 6-11k |

**GPU.** RTX 3060 Ti 8 GB works: driver 580, CUDA 13, docker nvidia runtime installed.
- Already used: photolib API ~2.5 GB, LibreOffice 5 x 130 MB. About 5 GB is free.
- The gate (qwen2.5-3b q4) and STT still run on the CPU.

**STT.** GigaAM v3 RNNT int8 (sherpa-onnx), CPU, greedy decoding, no hotwords.
- Input is one channel: the mean of 3 mics, after an AEC that only gets 2-4 dB.
- Phrase ends after 0.5 s of silence. Over music the audio is cut into 1.5 s windows.
- Real examples: «авита / ээлита» for Лиза; over music «педикюра на зрении ева ева что нави установи».
- The test set exists: docs/stt-test (truth.tsv, eval.py) and 108 phrases recorded on 19.11 on the Station (/var/lib/eva/stt-samples). Recording has stopped since 19.11.

**Alibaba Model Studio** (.env `#alibaba`, workspace in eu-central-1).
- The key is valid: `/models` lists 49 models (Qwen 3.5-4, DeepSeek V4, Kimi K3, GLM 5).
- Every chat call returns **403 AccessDenied.Unpurchased**, the old qwen-flash/qwen-plus included. Model Studio is not activated or paid in that region, or the workspace has no model access granted.
- Each answer took ~40 s, even the refusals. Check the network path to the EU endpoint (IPv6 first?).
- No ASR model in the list (qwen3-asr-flash is "model not exist" there).
- `#deploy portal com`: purpose unknown - ask.

**Station disk.** Root is 100% full (7 MB free): journal 168 MB, apt cache 92 MB, /root 94 MB. Writes of spend.json / history.json can fail. Fix first.

## Stage 0 - housekeeping (minutes)
1. Free the Station root:
   - `journalctl --vacuum-size=40M` plus a `SystemMaxUse=40M` drop-in;
   - `apt-get clean`;
   - look at /root.
2. Alibaba, on Иван's side in the console:
   - activate Model Studio for the EU region (pay-as-you-go or the free quota);
   - grant the workspace access to the models.

   Then re-run the check script: tool calls and latency.

## Stage A - local router on the GPU (the main cost cut)
1. llama.cpp server with CUDA on shn-linux. The model has to fit the ~5 GB that is free:
   - **Qwen3-4B-Instruct-2507 Q4_K_M** (~2.5 GB);
   - or a 7-8B Q4 (~4.7 GB) if LibreOffice is moved off the GPU.
   - Target: < 300 ms per classification. Replaces the CPU 3B gate.
2. One call per phrase, JSON constrained by a grammar:
   `{"to_eva": bool, "intent": <catalog id> | "claude" | "unclear", "args": {...}, "complete": bool}`.
3. The catalog, done locally with fixed spoken replies (cached TTS):

   | Group | Commands |
   |---|---|
   | music | play X (query passed straight to music_play), stop, pause, resume, next, previous, louder, quieter, «что играет» |
   | volume | set N %, louder, quieter |
   | screen | next, prev, open N, close; panels chat/stats; avatar |
   | home | «скажи <имя> …» (say_at_home), «передай <имя> …» (send_message) - template text |
   | time / date | |
   | unclear / garbage | «не расслышала», never sent to Claude |

4. Measure on the real phrases from /var/lib/eva/transcript before switching on: the share handled locally, mistakes. Expected: most voice requests never reach Claude.

## Stage B - a smaller, cheaper Claude path
1. Only the tools and skill prompts of the group the router picked (photos / finance / power / memory / home). 5.3k + 4k tokens become ~1-2k; the persona is trimmed.
2. Re-decide the cache TTL (5 min vs 1 h) on the new numbers: a smaller prefix and fewer calls change the balance.
3. Later: Opus as an orchestrator of small Sonnet sub-agents with only their skill's tools.

Target: cold request <= 2-3 c, warm < 0.5 c, $5-10 a month.

## Stage C - recognition
1. STT on the GPU, same HTTP API. Benchmark on docs/stt-test plus the 108 Station phrases plus new ones over music:
   - GigaAM v3 RNNT (now);
   - GigaAM + modified beam search + **hotwords** (Ева, Лиза, Соня, Маша, Иван, Эля…);
   - Whisper large-v3-turbo (GPU, names in the initial prompt);
   - the Alibaba ASR, if it becomes available.

   Compare WER, names right, latency.
2. Name correction: fuzzy match against the family dictionary (авита / ээлита -> Лиза).
3. Phrase ends:
   - END_SILENCE 0.5 s cuts phrases. Make it adaptive (0.8-1.0 s);
   - the router's `complete: false` waits for the rest instead of answering half a phrase.
4. Over music: after «Ева» the music is ducked. Record the whole phrase then instead of 1.5 s windows; fix the AEC reference alignment (TDM lag).
5. Recording of samples goes back on. Ship them to shn-linux, never to the Station disk.

## Stage D - who is speaking, by direction (mic array)
1. Find out what the hardware gives:
   - capture now has 3 mics (ch1-3) + ref; the Station 1 has more capsules - check for hidden channels or devices;
   - measure the geometry with a click / voice from known directions.
2. Direction of arrival per 32 ms frame (GCC-PHAT, cheap):
   - each phrase gets an azimuth;
   - a phrase is split where the direction jumps (never glue two people);
   - speaker = voice print + direction;
   - the TV's direction is learned and ignored.
3. Beamforming (delay-and-sum, then MVDR) to the active speaker: a cleaner signal for STT, partial suppression of the other voice and of the TV.
4. Real separation of two people talking at once with 3 mics is limited. Option:
   - send the raw 3-channel stream (96 KB/s) to shn-linux;
   - do the DOA, beamforming and neural separation (multi-channel SepFormer class) on the GPU there.

   This moves the ear's heavy part off the Station. Research step, after stages A-C.

## Order
0 -> A -> B, with C1 (benchmark) in parallel; then C2-C5, then D.

## Progress, 2026-11-23

**Stage 0 - done.** Why the Station root was full:

| What | Size |
|---|---|
| TTS cache, every phrase ever said kept as mp3 + wav | 174 MB |
| journal | 168 MB, mostly the eva-ui crash loop: cog restarted 2386 times overnight with the TV off (HDMI disconnected) |
| apt cache + lists | 185 MB |
| pip cache | 61 MB |

Fixes:
- journald is volatile (RAM, 24 MB). The new eva-logship service sends every entry to Postgres `assistant_memory.eva_log` on shn-linux; the old journal since 18.11 was imported (295k lines).
- TTS cache: wav only, LRU-capped at 40 MB.
- eva-ui waits for HDMI instead of restarting (drop-in wait-hdmi.conf); page fps logs once a minute.
- STT samples and spk-test moved to shn-linux ~/eva-data.
- Root: 7 MB -> 580 MB free.

**Stage A - done.** eva-router on shn-linux :8906 (llama.cpp server-cuda, Qwen3-4B-Instruct-2507 Q4_K_M, ~3.2 GB VRAM).
- Median 168 ms on 427 real phrases.
- Not-for-Eva phrases: 194/194 ignored.
- Catalog commands are done locally in dialog.local(): music, volume, screen, avatar, panels, time, ack, «не расслышала».
- app/core/router.py; the old CPU gate is the fallback.

**Stage B - postponed a day:** measure what Claude still costs with the router in place.

**Stage C.** Benchmark on the 47-phrase quiet test set:

| Engine | WER | Names |
|---|---|---|
| GigaAM v3 RNNT + names.fix (in use) | 14.0% | 29/33 |
| hotwords + fix | 15.4% | 32/33 (0.94 s CPU, 1.9 s GPU) |
| Whisper large-v3-turbo GPU + prompt | 23.2% | 28/33 |

The model stays. The real losses are the front end; fixed:
- 1.5 s windows only while her own voice plays, no longer over music;
- END_SILENCE 0.5 -> 0.65 s;
- 0.3 s of tail kept, endings were cut.

The STT server now keeps every phrase (wav + text) on shn-linux ~/eva-data/stt-log, 14 days: the real home test set.

**Stage D finding.** The mic board has five TLV320ADC3101 stereo ADCs (i2c 0-0018, 1-0018..001b), up to 10 channels. The device tree routes only 5 TDM slots to capture (3 mics + 2 taps). All the mics need a DTB change in the Station's boot partition - a separate, careful step.

**`#deploy portal com`** in .env = deploy.com API key (DeepSeek V4.1 Flash), for the model comparison later.

## Stage D progress, 2026-11-23 (afternoon)

**No DTB change needed.** The CPU side of I2S runs 8 x 32-bit slots; the codecs run 16 x 16-bit. So each 32-bit capture word carries TWO codec channels.

Mapped by muting each ADC channel in turn:

| Word | High half | Low half |
|---|---|---|
| 0 | ADC4 R tap | ADC4 L tap |
| 1-3 | ADC0-2 R mics | ADC0-2 L mics |
| 4 | ADC3 R (amplifier tap = AEC reference) | ADC3 L mic |

The ear used only the high halves of words 1-3: 3 of the 7 microphones. Now:
- app/ear.py `unpack()` reads all 7 mics + the reference.

Mic gain:
- measured on the taps, the ADC's own noise is 0.85 LSB;
- at the vendor's 20 dB the mic noise was 3.5x below it, so ~10 dB of SNR was lost in the conversion;
- the PGA is now 36 dB (station-mics-init.sh: 72); MIC_GAIN in the ear takes the 16 dB back out.

**Direction:**
- stt_server `/doa`: GCC-PHAT delays of all 21 mic pairs = a direction fingerprint, for the whole phrase and per 0.5 s block; no geometry needed;
- the ear sends each phrase's raw 7 channels, so phrases carry `doa`.

Next:
- look at real phrases (eva_log "doa" lines + ~/eva-data/stt-log);
- then use it in the brain: split / attribute by direction, ignore the TV's direction;
- beamforming needs the geometry: fit it from the delays (MDS).

**Regression found and fixed.** The in-phrase speaker split (`stt_server.speaker_change`, written 19.11) had been put into the running container with `docker cp` only, never into the source or the image.
- Recreating the container today lost it; the repo file had also been overwritten by an older copy.
- Restored from the session transcripts (4 edits replayed onto the v3 source).
- Now it is in station1/work/stt-docker/stt_server.py and image newalisa-stt:v4.
- Never `docker cp` code into a container.
