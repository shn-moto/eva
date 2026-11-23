#!/usr/bin/env bash
# Run every model (or the ones given) one after another, each in its own process.
#   docker run --rm -v <stt-log>:/log:ro -v <out>:/out -v <cache>:/root/.cache eva-emotion-bench [model ...]
models=${*:-gigaam-emo emotion2vec audeering-dim ast-audioset}
for m in $models; do
    echo "== $m"
    python bench.py "$m" /log /out --threads "${THREADS:-4}" || echo "$m FAILED"
done
