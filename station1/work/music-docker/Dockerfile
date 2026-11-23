FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg deno && pip install --no-cache-dir -U "yt-dlp[default]" numpy
COPY music_server.py /app/music_server.py
CMD ["python", "-u", "/app/music_server.py"]
