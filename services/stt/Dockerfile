FROM python:3.12-slim

# versions pinned to what ran in newalisa-stt:v4 (2026-11-23); yt-dlp-style
# "latest" made the image change under the same Dockerfile
RUN pip install --no-cache-dir faster-whisper==1.2.1 sherpa-onnx==2.0.1 fastapi==0.162.0 \
        "uvicorn[standard]==0.54.0" numpy==2.5.4 "psycopg[binary]==3.2.10" \
        livekit==1.3.0 onnxruntime==1.32.0
# livekit: WebRTC's audio processing (AEC3, noise suppression) for the ear (ear_stream.py)

WORKDIR /app
COPY stt_server.py voices.py hear.py rescan.py ear_stream.py ear_null.py silero_vad.onnx doa_geometry.json /app/

ENV STT_MODEL=small STT_COMPUTE=int8 STT_THREADS=8
EXPOSE 8901 8912
CMD ["uvicorn", "stt_server:app", "--host", "0.0.0.0", "--port", "8901"]
