FROM python:3.12-alpine
# yt-dlp breaks whenever YouTube changes its player: 2026.10.26 could not solve the n-challenge
# and every such track was "This video is not available" (2026-11-27, the Leopold song).
# The floor busts the build cache; music_server.py also updates it daily while running.
RUN apk add --no-cache ffmpeg deno && pip install --no-cache-dir -U "yt-dlp[default]>=2026.11.23" yt-dlp-ejs numpy
COPY music_server.py /app/music_server.py
CMD ["python", "-u", "/app/music_server.py"]
