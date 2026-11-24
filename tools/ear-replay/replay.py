#!/usr/bin/env python3
"""The hub's ear (/hear/fragment?dry=1) on the STT log's clips, against what
the Station's ear made of them then (assistant_memory.utterances).

Run on shn-linux (stdlib only):
    python3 replay.py [days=7] [limit] > report.txt

For every clip with its raw mics and phrases in the memory: the parts the hub
makes, who it says each part is, and the text - next to the Station's parts and
speakers, and next to the person's correction where there is one. Dry: nothing
is logged, nothing learnt. The voice base already holds prints learnt from some
of these clips, so "who" is a little optimistic.
"""
import collections
import json
import os
import subprocess
import sys
import urllib.request
import wave

LOG = os.path.expanduser("~/eva-data/stt-log")
HUB = "http://127.0.0.1:8901/hear/fragment?dry=1"
days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10 ** 9

rows = subprocess.run(
    ["docker", "exec", "smart_photo_db", "psql", "-U", "dev", "-d", "assistant_memory", "-AtF", "\t", "-c",
     "SELECT audio, coalesce(speaker,''), coalesce(speaker_heard,''), corrected_at IS NOT NULL, "
     "replace(coalesce(text_heard, text), chr(9), ' '), replace(text, chr(9), ' ') FROM utterances "
     f"WHERE role = 'user' AND audio IS NOT NULL AND ts > now() - interval '{days} days' ORDER BY id"],
    capture_output=True, text=True, check=True).stdout.splitlines()
clips = collections.OrderedDict()
for r in rows:
    audio, spk, heard, corr, text_heard, text = r.split("\t")
    base, _, span = audio.partition("#t=")
    clips.setdefault(base, []).append({"span": span, "speaker": spk, "heard_speaker": heard or spk,
                                       "corrected": corr == "t", "text": text_heard, "fixed": text})


def read(path):
    with wave.open(path) as w:
        return w.readframes(w.getnframes()), w.getnchannels(), w.getnframes()


stats = collections.Counter()
for base, ph in clips.items():
    if stats["clips"] >= limit:
        break
    mono_p, mics_p = os.path.join(LOG, base + ".wav"), os.path.join(LOG, base + "-mics.wav")
    if not (os.path.exists(mono_p) and os.path.exists(mics_p)):
        continue
    mono, _, n = read(mono_p)
    mics, ch, _ = read(mics_p)
    meta = {"dur": round(n / 16000, 2), "start": 0.0, "speech_start": 0.0, "end": round(n / 16000, 2),
            "playback": 0.0, "residual": 1.0}
    req = urllib.request.Request(HUB, data=mono + mics, headers={
        "X-Meta": json.dumps(meta), "X-Mono": str(n), "X-Channels": str(ch)})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            evs = json.loads(r.read())["events"]
    except Exception as e:
        print(base, "ERROR", e)
        continue
    hub = [e["item"] for e in evs if e["type"] == "utterance"]
    stats["clips"] += 1
    stats["station parts"] += len(ph)
    stats["hub parts"] += len(hub)
    if len(ph) != len(hub):
        stats["parts differ"] += 1
    lines = []
    # who: part by part in order (the same cuts) or the whole's main speaker
    for k, p in enumerate(ph):
        h = hub[k] if k < len(hub) and len(hub) == len(ph) else (hub[0] if len(hub) == 1 else None)
        hs = ((h or {}).get("speaker") or {}).get("label", "-") if h else "-"
        truth = p["speaker"]
        if p["corrected"]:
            stats["corrected"] += 1
            stats["corrected: station right"] += p["heard_speaker"] == truth
            stats["corrected: hub right"] += hs == truth
        else:
            stats["uncorrected"] += 1
            stats["uncorrected: hub = station"] += hs == truth
        mark = "" if hs == truth else "  <-- " + ("FIXED" if p["corrected"] and p["heard_speaker"] != truth and hs == truth
                                                   else "differs")
        if p["corrected"] and p["heard_speaker"] != truth and hs != truth:
            mark = "  <-- still wrong"
        lines.append(f"   station {p['heard_speaker']:8s} truth {truth:8s}{' (corr)' if p['corrected'] else '       '}"
                     f" hub {hs:8s}{mark}  {p['text'][:50]}")
    if len(ph) != len(hub) or any("<--" in l for l in lines):
        print(f"{base}: station {len(ph)} part(s), hub {len(hub)}: "
              + " | ".join(f"{h.get('dur')}s {(h.get('speaker') or {}).get('label', '-')}: {h.get('raw', '')[:40]!r}" for h in hub))
        print("\n".join(lines))
print()
for k, v in stats.items():
    print(f"{k:30s} {v}")
