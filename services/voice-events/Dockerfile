# What a sound is (PANNs CNN14) and how speech sounds (GigaAM-Emo), docs/EMOTION-PLAN.md, on
# the CPU. The checkpoints are not in the image: the cache volume (~/eva-data/bench-cache ->
# /root/.cache) holds panns_data/ and gigaam/emo.ckpt.
FROM python:3.11-slim
# ffmpeg: GigaAM reads its audio through it
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir panns_inference fastapi==0.162.0 "uvicorn[standard]==0.54.0" numpy \
    git+https://github.com/salute-developers/GigaAM.git
# PANNs looks for its checkpoint and labels in ~/panns_data
RUN ln -s /root/.cache/panns_data /root/panns_data
WORKDIR /app
COPY events_server.py /app/
EXPOSE 8911
CMD ["uvicorn", "events_server:app", "--host", "0.0.0.0", "--port", "8911"]
