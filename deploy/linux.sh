#!/usr/bin/env bash
# Deploy Eva's services on shn-linux from git - a commit that is on origin.
# Run from Git Bash in the repo root:
#
#   deploy/linux.sh stt|music|router|gpu-arbiter|admin|print|brain [ref]      # default origin/main
#
# The source of the service is taken from the commit (git archive), built into
# an image tagged with the commit, and the container is recreated with the run
# settings below. Never `docker cp` code into a container: it is lost on the
# next recreate (the speaker split was, 2026-11-23).
# Models and data stay on the server (bind mounts), not in git.
set -euo pipefail

# hosts and keys: deploy/local.env (not in git, see local.env.example)
[ -f deploy/local.env ] && { set -a; . deploy/local.env; set +a; }
HOST=${HOST:?set HOST in deploy/local.env}
HUB_KEY=${HUB_KEY:?set HUB_KEY in deploy/local.env}
HUB_KEY=${HUB_KEY/#\~/$HOME}
SSH=(ssh -i "$HUB_KEY" -o ConnectTimeout=10 "$HOST")

svc=${1:?service: stt | music | router | gpu-arbiter | admin | print | brain}
ref=${2:-origin/main}
git fetch -q origin
sha=$(git rev-parse --verify "$ref^{commit}")
git branch -r --contains "$sha" | grep -q "origin/" || { echo "commit $sha is not on origin - push it first" >&2; exit 1; }
short=$(git rev-parse --short "$sha")

case "$svc" in
stt)
    src=services/stt; image=newalisa-stt; name=newalisa-stt; port=8901
    # ~/eva-data/admin.env: ASSISTANT_PG_DSN - the voice base (services/stt/voices.py)
    # 8912: the Station's raw sound stream (ear_stream.py)
    run="-p 8901:8901 -p 8912:8912 --add-host=host.docker.internal:host-gateway --env-file \$HOME/eva-data/admin.env -e STT_ENGINE=gigaam-rnnt -e GIGAAM_DIR=/models/giga3-rnnt -e STT_THREADS=8 \
-e STT_LOG_DIR=/log -v /etc/localtime:/etc/localtime:ro \
-v \$HOME/newalisa-stt-v3/models:/models:ro -v \$HOME/eva-data/stt-log:/log \
-v \$HOME/eva-data/stt-keep:/keep:ro"
    # /keep: the admin page's settings - the places marked there (the TV: services/stt/hear.py)
    cmd="uvicorn stt_server:app --host 0.0.0.0 --port 8901"
    health=/health ;;
voice-events)
    # what a sound is (docs/EMOTION-PLAN.md): PANNs CNN14 on the CPU, its checkpoint in the cache
    src=services/voice-events; image=eva-voice-events; name=eva-voice-events; port=8911
    run="-p 8911:8911 --memory 4g -e THREADS=4 -v \$HOME/eva-data/bench-cache:/root/.cache:ro"
    cmd=""; health=/health ;;
music)
    src=services/music; image=newalisa-music; name=newalisa-music; port=8904
    # /media: the hub's own audio files (a listener names them /media/<name>); /cache: YouTube tracks
    # played once, as mp3 - YouTube took ~9 s to the first sound (2026-11-27)
    run="-p 8904:8904 -v /etc/localtime:/etc/localtime:ro -v /media:/media:ro -v \$HOME/eva-data/music-cache:/cache"; cmd=""; health=/health ;;
admin)
    # the admin page: phrases with their sound, logs, search, keep / delete (home network only).
    # ~/eva-data/admin.env (not in git): ASSISTANT_PG_DSN=...
    src=services/admin; image=eva-admin; name=eva-admin; port=8908
    run="-p 8908:8908 --add-host=host.docker.internal:host-gateway --env-file \$HOME/eva-data/admin.env -e STT_LOG_DIR=/log -e KEEP_DIR=/keep \
-e PGTZ=\$(cat /etc/timezone) -v /etc/localtime:/etc/localtime:ro \
-v \$HOME/eva-data/stt-log:/log -v \$HOME/eva-data/stt-keep:/keep -v \$HOME/eva-data/brain/data:/brain -v /media:/media"
    # /brain: the brain's calendar and listeners, edited on the page (app/core/calendar.py, listeners.py)
    cmd=""; health=/api/days ;;
print)
    # files sent to Eva in Telegram (kept a week) + printing through the home's CUPS
    # (cups_airprint, host network, its default queue)
    src=services/print; image=eva-print; name=eva-print; port=8909
    run="--network host -e CUPS_SERVER=127.0.0.1:631 -e PHOTOLIB_URL=http://127.0.0.1:8000 -v /etc/localtime:/etc/localtime:ro -v \$HOME/eva-data/print-inbox:/files"
    cmd=""; health=/health ;;
brain)
    # Eva's brain (docs/BRAIN-PLAN.md): app/ in a container, Telegram next to it.
    # Host network: the hub's services on 127.0.0.1, the Station's body on the LAN.
    # ~/eva-data/brain/{data,eva,cache,secrets.env} (not in git): the state that
    # was /var/lib/eva on the Station, the private profile, the TTS cache, secrets.
    src=app; image=eva-brain; name=eva-brain; port=8081; dockerfile=brain.Dockerfile
    STATION=${STATION:?set STATION in deploy/local.env}
    run="--network host -e EVA_BODY_URL=http://${STATION#*@}:8085 -e EVA_HEAR_EVENTS=1 -e EVA_LOG_UNIT=eva-brain \
-v /etc/localtime:/etc/localtime:ro -v \$HOME/eva-data/brain/data:/var/lib/eva -v \$HOME/eva-data/brain/eva:/opt/eva/eva:ro \
-v \$HOME/eva-data/brain/cache:/var/cache/eva -v \$HOME/eva-data/brain/secrets.env:/etc/eva/secrets.env:ro"
    cmd=""; health=/health ;;
gpu-arbiter)
    # stops eva-router while photolib indexes (indexing gets the whole GPU), starts it after
    src=services/gpu-arbiter; image=eva-gpu-arbiter; name=eva-gpu-arbiter; port=8907
    run="--network host -v /var/run/docker.sock:/var/run/docker.sock -v \$HOME/eva-data/arbiter:/state \
-e PHOTOLIB_URL=http://127.0.0.1:8000 -e CONTAINERS=eva-router"
    cmd=""; health=/health ;;
router)
    # no source of ours: the prebuilt llama.cpp CUDA server + a model on the server.
    # 8080 inside: the image's own healthcheck asks there (on 8906 docker said "unhealthy")
    image=ghcr.io/ggml-org/llama.cpp:server-cuda; name=eva-router; port=8906
    "${SSH[@]}" "docker rm -f $name >/dev/null 2>&1; docker run -d --name $name --restart unless-stopped --gpus all \
-p 8906:8080 -v \$HOME/eva-router/models:/models:ro $image \
-m /models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf --host 0.0.0.0 --port 8080 -ngl 99 -c 4096 -np 1 --cache-reuse 256 >/dev/null \
&& for i in \$(seq 60); do curl -sf localhost:$port/health >/dev/null && echo $name up && exit 0; sleep 2; done; echo $name NOT up; exit 1"
    exit $? ;;
*) echo "unknown service $svc" >&2; exit 1 ;;
esac

echo "deploying $svc at $short ($(git log -1 --format=%s "$sha"))"
if [ "$svc" = brain ]; then
    # never in the middle of her answer: a restart lost a drawn picture and the turn
    # with it, and the next «распечатай» printed an old photo (2026-11-25)
    "${SSH[@]}" 'for i in $(seq 90); do curl -sf localhost:8081/health | grep -q "\"busy\": true" || exit 0; sleep 2; done; echo "brain still busy after 3 min - deploying anyway"'
fi
git archive "$sha" "$src" | "${SSH[@]}" "rm -rf ~/eva-src/$svc && mkdir -p ~/eva-src/$svc && tar -x -C ~/eva-src/$svc --strip-components=$(echo "$src" | tr -cd / | wc -c | awk '{print $1+1}')"
"${SSH[@]}" "cd ~/eva-src/$svc && echo $sha > DEPLOYED && docker build -q -f ${dockerfile:-Dockerfile} -t $image:$short -t $image:latest . >/dev/null \
&& docker rm -f $name >/dev/null 2>&1; docker run -d --name $name --restart unless-stopped $run $image:$short $cmd >/dev/null \
&& for i in \$(seq 60); do curl -sf localhost:$port$health >/dev/null && echo $name up at $short && exit 0; sleep 2; done; echo $name NOT up; docker logs --tail 20 $name; exit 1"

if [ "$svc" = print ]; then
    # office files -> PDF by the host's own LibreOffice: convert_host.py on the host's
    # system Python, kept alive by the user's crontab (no sudo needed)
    "${SSH[@]}" 'sh ~/eva-src/print/install_host.sh'
fi
