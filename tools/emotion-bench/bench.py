#!/usr/bin/env python3
"""Benchmark speech-emotion and sound-event models on Eva's STT log (CPU).

    python bench.py MODEL LOG_DIR OUT_DIR [--threads 4]

MODEL: gigaam-emo | emotion2vec | audeering-dim | ast-audioset | panns-cnn14
LOG_DIR: the STT log (<date>/<time>.wav + .json, mono 16 kHz; the 7-channel
         "-raw7" recordings are skipped), or ESC-50 (its class becomes the text;
         env CLASSES=a,b,c takes only those classes). Other rates are resampled.
Writes OUT_DIR/<model>.jsonl (one line per phrase) and OUT_DIR/<model>.meta.json
(load time, peak memory, seconds per phrase, real-time factor).

One model per process, so the peak memory is that model's own.
"""
import glob
import json
import os
import resource
import sys
import time
import wave

import numpy as np
import torch

# AudioSet classes that matter for the scenarios (crying, screaming, laughter)
# plus the ones that explain false alarms (music, TV).
EVENTS = ["Speech", "Child speech, kid speaking", "Crying, sobbing", "Baby cry, infant cry", "Whimper",
          "Screaming", "Shout", "Yell", "Children shouting", "Laughter", "Baby laughter", "Giggle",
          "Music", "Television", "Silence"]


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def read(path):
    with wave.open(path) as w:
        ch, rate = w.getnchannels(), w.getframerate()
        if ch != 1:
            return None
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if rate != 16000:                   # public sets (ESC-50: 44.1 kHz)
        import librosa
        a = librosa.resample(a, orig_sr=rate, target_sr=16000)
    return a


def esc50_labels(log_dir):
    """ESC-50 layout (<root>/audio/*.wav + <root>/meta/esc50.csv): file -> category."""
    import csv
    meta = os.path.join(log_dir, "meta", "esc50.csv")
    if not os.path.exists(meta):
        return {}
    return {r["filename"]: r["category"] for r in csv.DictReader(open(meta))}


def phrases(log_dir):
    labels = esc50_labels(log_dir)
    only = set(filter(None, os.environ.get("CLASSES", "").split(",")))
    for wav in sorted(glob.glob(os.path.join(log_dir, "**", "*.wav"), recursive=True)):
        if "-raw" in os.path.basename(wav):
            continue
        cls = labels.get(os.path.basename(wav))
        if only and cls not in only:
            continue
        audio = read(wav)
        if audio is None or len(audio) < 8000:     # < 0.5 s: nothing to judge
            continue
        info = {}
        try:
            info = json.load(open(wav[:-4] + ".json", encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if cls:
            info["text"] = "[" + cls + "]"
        yield wav, audio, info


# ---- models: load() -> fn(path, audio) -> dict ----

def gigaam_emo():
    import gigaam
    m = gigaam.load_model("emo", device="cpu")

    def run(path, audio):
        p = m.get_probs(path)
        top = max(p, key=p.get)
        return {"label": top, "score": round(float(p[top]), 3), "probs": {k: round(float(v), 3) for k, v in p.items()}}
    return run


def emotion2vec():
    from funasr import AutoModel
    m = AutoModel(model="emotion2vec/emotion2vec_plus_base", hub="hf", disable_update=True, device="cpu")

    def run(path, audio):
        r = m.generate(audio, granularity="utterance", extract_embedding=False, disable_pbar=True)[0]
        labels = [l.split("/")[-1] for l in r["labels"]]
        probs = {l: round(float(s), 3) for l, s in zip(labels, r["scores"])}
        top = max(probs, key=probs.get)
        return {"label": top, "score": probs[top], "probs": probs}
    return run


def audeering_dim():
    import torch.nn as nn
    from transformers import Wav2Vec2Processor
    from transformers.models.wav2vec2.modeling_wav2vec2 import Wav2Vec2Model, Wav2Vec2PreTrainedModel

    class Head(nn.Module):
        def __init__(self, config):
            super().__init__()
            self.dense = nn.Linear(config.hidden_size, config.hidden_size)
            self.dropout = nn.Dropout(config.final_dropout)
            self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

        def forward(self, x):
            return self.out_proj(self.dropout(torch.tanh(self.dense(self.dropout(x)))))

    class EmotionModel(Wav2Vec2PreTrainedModel):
        def __init__(self, config):
            super().__init__(config)
            self.wav2vec2 = Wav2Vec2Model(config)
            self.classifier = Head(config)
            self.post_init()

        def forward(self, input_values):
            return self.classifier(self.wav2vec2(input_values)[0].mean(dim=1))

    name = "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"
    proc = Wav2Vec2Processor.from_pretrained(name)
    m = EmotionModel.from_pretrained(name).eval()

    def run(path, audio):
        x = proc(audio, sampling_rate=16000, return_tensors="pt").input_values
        with torch.no_grad():
            a, d, v = m(x)[0].tolist()       # arousal, dominance, valence, 0..1
        return {"arousal": round(a, 3), "dominance": round(d, 3), "valence": round(v, 3)}
    return run


def ast_audioset():
    from transformers import pipeline
    clf = pipeline("audio-classification", model="MIT/ast-finetuned-audioset-10-10-0.4593", device="cpu", top_k=None)

    def run(path, audio):
        r = {x["label"]: x["score"] for x in clf({"raw": audio, "sampling_rate": 16000})}
        top = sorted(r.items(), key=lambda kv: -kv[1])[:3]
        return {"top": [[k, round(v, 3)] for k, v in top], "events": {k: round(r.get(k, 0.0), 3) for k in EVENTS}}
    return run


def panns_cnn14():
    """PANNs CNN14 (AudioSet, 32 kHz): a CNN, its time grows with the clip, unlike AST."""
    import librosa
    from panns_inference import AudioTagging, labels
    m = AudioTagging(checkpoint_path=None, device="cpu")

    def run(path, audio):
        x = librosa.resample(audio, orig_sr=16000, target_sr=32000)[None, :]
        p, _ = m.inference(x)
        r = dict(zip(labels, p[0].tolist()))
        top = sorted(r.items(), key=lambda kv: -kv[1])[:3]
        return {"top": [[k, round(v, 3)] for k, v in top], "events": {k: round(r.get(k, 0.0), 3) for k in EVENTS}}
    return run


MODELS = {"gigaam-emo": gigaam_emo, "emotion2vec": emotion2vec, "audeering-dim": audeering_dim,
          "ast-audioset": ast_audioset, "panns-cnn14": panns_cnn14}


def main():
    args = sys.argv[1:]
    threads = 4
    if "--threads" in args:
        i = args.index("--threads")
        threads = int(args[i + 1])
        del args[i:i + 2]
    name, log_dir, out_dir = args
    torch.set_num_threads(threads)
    os.makedirs(out_dir, exist_ok=True)

    t = time.time()
    run = MODELS[name]()
    load_s = time.time() - t
    load_mb = rss_mb()

    n, audio_s, spent, times = 0, 0.0, 0.0, []
    with open(os.path.join(out_dir, name + ".jsonl"), "w", encoding="utf-8") as out:
        for path, audio, info in phrases(log_dir):
            t = time.time()
            try:
                res = run(path, audio)
            except Exception as e:      # one bad clip must not stop the run
                res = {"error": str(e)[:200]}
            dt = time.time() - t
            if n:                       # the first call warms up, keep it out of the average
                spent += dt
                times.append(dt)
            n += 1
            audio_s += len(audio) / 16000
            out.write(json.dumps(dict(file=os.path.relpath(path, log_dir), text=info.get("text", ""),
                                      sec=round(len(audio) / 16000, 2), ms=int(dt * 1000), **res),
                                 ensure_ascii=False) + "\n")
    avg = audio_s / n if n else 0
    meta = {"model": name, "threads": threads, "phrases": n, "audio_s": round(audio_s, 1),
            "load_s": round(load_s, 1), "rss_after_load_mb": round(load_mb), "rss_peak_mb": round(rss_mb()),
            "s_per_phrase": round(spent / max(n - 1, 1), 3),
            "p95_s": round(sorted(times)[int(len(times) * 0.95)] if times else 0, 3),
            "rtf": round(spent / max(audio_s - avg, 1e-9), 4)}
    json.dump(meta, open(os.path.join(out_dir, name + ".meta.json"), "w"), indent=1)
    print(json.dumps(meta))


if __name__ == "__main__":
    main()
