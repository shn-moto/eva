# CPU-only bench image for speech-emotion / sound-event models (see bench.py).
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git wget && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir transformers funasr modelscope soundfile librosa \
    git+https://github.com/salute-developers/GigaAM.git panns_inference
# PANNs keeps its checkpoint and labels in ~/panns_data: point it at the cache volume
RUN ln -s /root/.cache/panns_data /root/panns_data
WORKDIR /bench
COPY bench.py run.sh ./
ENTRYPOINT ["bash", "run.sh"]
