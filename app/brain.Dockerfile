# Eva's brain on shn-linux (docs/BRAIN-PLAN.md): dialog, Claude, skills, TTS,
# Telegram. The body (speaker, screen, ring, keys) stays on the Station and is
# driven over the LAN (core/body.py). Built by deploy/linux.sh brain from app/.
FROM python:3.13-slim

# mpg123: the TTS's mp3 -> wav (say.tts); the sound is played by the body
RUN apt-get update && apt-get install -y --no-install-recommends mpg123 curl && rm -rf /var/lib/apt/lists/*
# versions as in the Station's venv when the brain moved (2026-11-25)
RUN pip install --no-cache-dir anthropic==2.10.0 numpy==2.2.4 "psycopg[binary]==3.3.9"
# pillow: the artist skill sizes its pictures (a small copy for her eyes, PNG for a redraw)
RUN pip install --no-cache-dir pillow==11.3.0

WORKDIR /opt/eva/app
COPY . /opt/eva/app/
ENV PYTHONUNBUFFERED=1

# the brain, and the Telegram channel next to it (it talks to the brain on 127.0.0.1:8081)
CMD ["sh", "-c", "while true; do python telegram.py; sleep 3; done & exec python brain.py"]
