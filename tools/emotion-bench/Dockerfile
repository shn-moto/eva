# CPU-only bench image for speech-emotion / sound-event models (see bench.py).
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir transformers funasr modelscope soundfile librosa \
    git+https://github.com/salute-developers/GigaAM.git
WORKDIR /bench
COPY bench.py run.sh ./
ENTRYPOINT ["bash", "run.sh"]
