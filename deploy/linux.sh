#!/usr/bin/env bash
# Deploy Eva's services on shn-linux from git - a commit that is on origin.
# Run from Git Bash in the repo root:
#
#   deploy/linux.sh stt|music|router|gpu-arbiter|admin|print [ref]      # default origin/main
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

svc=${1:?service: stt | music | router | gpu-arbiter | admin | print}
ref=${2:-origin/main}
git fetch -q origin
sha=$(git rev-parse --verify "$ref^{commit}")
git branch -r --contains "$sha" | grep -q "origin/" || { echo "commit $sha is not on origin - push it first" >&2; exit 1; }
short=$(git rev-parse --short "$sha")

case "$svc" in
stt)
    src=services/stt; image=newalisa-stt; name=newalisa-stt; port=8901
    run="-p 8901:8901 -e STT_ENGINE=gigaam-rnnt -e GIGAAM_DIR=/models/giga3-rnnt -e STT_THREADS=8 \
-e STT_LOG_DIR=/log -v /etc/localtime:/etc/localtime:ro \
-v \$HOME/newalisa-stt-v3/models:/models:ro -v \$HOME/eva-data/stt-log:/log"
    cmd="uvicorn stt_server:app --host 0.0.0.0 --port 8901"
    health=/health ;;
music)
    src=services/music; image=newalisa-music; name=newalisa-music; port=8904
    run="-p 8904:8904"; cmd=""; health=/health ;;
admin)
    # the admin page: phrases with their sound, logs, search, keep / delete (home network only).
    # ~/eva-data/admin.env (not in git): ASSISTANT_PG_DSN=...
    src=services/admin; image=eva-admin; name=eva-admin; port=8908
    run="-p 8908:8908 --env-file \$HOME/eva-data/admin.env -e STT_LOG_DIR=/log -e KEEP_DIR=/keep \
-e PGTZ=\$(cat /etc/timezone) -v /etc/localtime:/etc/localtime:ro \
-v \$HOME/eva-data/stt-log:/log -v \$HOME/eva-data/stt-keep:/keep"
    cmd=""; health=/api/days ;;
print)
    # files sent to Eva in Telegram (kept a week) + printing through the home's CUPS
    # (cups_airprint, host network, its default queue)
    src=services/print; image=eva-print; name=eva-print; port=8909
    run="--network host -e CUPS_SERVER=127.0.0.1:631 -e PHOTOLIB_URL=http://127.0.0.1:8000 -v /etc/localtime:/etc/localtime:ro -v \$HOME/eva-data/print-inbox:/files"
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
git archive "$sha" "$src" | "${SSH[@]}" "rm -rf ~/eva-src/$svc && mkdir -p ~/eva-src/$svc && tar -x -C ~/eva-src/$svc --strip-components=$(echo "$src" | tr -cd / | wc -c | awk '{print $1+1}')"
"${SSH[@]}" "cd ~/eva-src/$svc && echo $sha > DEPLOYED && docker build -q -t $image:$short -t $image:latest . >/dev/null \
&& docker rm -f $name >/dev/null 2>&1; docker run -d --name $name --restart unless-stopped $run $image:$short $cmd >/dev/null \
&& for i in \$(seq 60); do curl -sf localhost:$port$health >/dev/null && echo $name up at $short && exit 0; sleep 2; done; echo $name NOT up; docker logs --tail 20 $name; exit 1"
