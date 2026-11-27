#!/usr/bin/env bash
# Deploy Eva to the Station from git - a commit that is on origin, never the
# working tree. Run from Git Bash in the repo root:
#
#   deploy/station.sh                 # origin/main
#   deploy/station.sh <ref>           # a tag / commit (must be on origin)
#   deploy/station.sh --diff [ref]    # only show which files would change
#   FORCE=1 deploy/station.sh         # restart even while music plays
#
# What goes where (git archive | tar on the Station):
#   app/                                    -> /opt/eva/app
#   ui/spike/                               -> /opt/eva/ui/spike
#   (eva/ = the household profile is private: /opt/eva/eva is never touched)
#   os/rootfs/files/{etc,usr}/...eva...     -> /                   (units, audio init)
# Secrets stay on the device (/etc/eva/secrets.env), never in git.
# /opt/eva/DEPLOYED records the commit. Files deleted in git are not deleted
# on the Station (listed by --diff as "only on the Station").
set -euo pipefail

# hosts and keys: deploy/local.env (not in git, see local.env.example)
[ -f deploy/local.env ] && { set -a; . deploy/local.env; set +a; }
STATION=${STATION:?set STATION in deploy/local.env}
KEY=${KEY:?set KEY in deploy/local.env}
SSH=(ssh -i "$KEY" -o ConnectTimeout=10 "$STATION")
PARTS=(app ui/spike)
ROOTFS=os/rootfs/files

diff_only=0
if [ "${1:-}" = "--diff" ]; then diff_only=1; shift; fi
ref=${1:-origin/main}

git fetch -q origin
sha=$(git rev-parse --verify "$ref^{commit}")
if ! git branch -r --contains "$sha" | grep -q "origin/"; then
    echo "commit $sha is not on origin - push it first" >&2
    exit 1
fi
short=$(git rev-parse --short "$sha")

# which files differ from what the Station has now
changed=$(
    for p in "${PARTS[@]}"; do
        git ls-tree -r "$sha" --name-only -- "$p"
    done | while read -r f; do
        want=$(git show "$sha:$f" | md5sum | cut -c1-32)
        echo "$want  /opt/eva/$f"
    done | "${SSH[@]}" 'md5sum -c --quiet - 2>/dev/null | sed "s/: .*//"' || true
)
rootfs_changed=$(
    git ls-tree -r "$sha" --name-only -- "$ROOTFS" | while read -r f; do
        want=$(git show "$sha:$f" | md5sum | cut -c1-32)
        echo "$want  /${f#$ROOTFS/}"
    done | "${SSH[@]}" 'md5sum -c --quiet - 2>/dev/null | sed "s/: .*//"' || true
)
echo "deploying $short ($(git log -1 --format=%s "$sha"))"
echo "changed on the Station:"
printf '  %s\n' $changed $rootfs_changed
[ -z "$changed$rootfs_changed" ] && echo "  (nothing)"
[ "$diff_only" = 1 ] && exit 0
[ -z "$changed$rootfs_changed" ] && { echo "up to date"; exit 0; }

if [ "${FORCE:-0}" != 1 ] && "${SSH[@]}" '[ -e /run/eva/music ]'; then
    echo "music is playing - not restarting now (FORCE=1 to do it anyway)" >&2
    exit 1
fi

git archive "$sha" "${PARTS[@]}" | "${SSH[@]}" 'tar -x -C /opt/eva'
git archive "$sha" "$ROOTFS" | "${SSH[@]}" "tar -x -C / --strip-components=3 --no-overwrite-dir"
# a drop-in once put on the device by hand (never in git): it held eva-ui in start-pre
# for days with the TV off; eva-ui + eva-hdmi do that job now (2026-11-27)
"${SSH[@]}" "rm -f /etc/systemd/system/eva-ui.service.d/wait-hdmi.conf; rmdir /etc/systemd/system/eva-ui.service.d 2>/dev/null; true"
"${SSH[@]}" "echo '$sha $(date -u +%FT%TZ)' > /opt/eva/DEPLOYED; systemctl daemon-reload"
# units added since (the body 2026-11-25, the HDMI watch 2026-11-27) start with the next boot too
for u in eva-body eva-hdmi; do
    "${SSH[@]}" "systemctl is-enabled -q $u 2>/dev/null || systemctl enable -q $u"
done

# restart what the change touches
units=()
# the brain and Telegram run on shn-linux (deploy/linux.sh brain); here only the ear,
# the body (below) and the log shipper
echo "$changed" | grep -q "/opt/eva/app/" && units+=(eva-ear eva-logship)
# the body only when its own code changed: a restart of it cuts her voice and the music
echo "$changed" | grep -q "/opt/eva/app/body.py\|/opt/eva/app/say.py\|/opt/eva/app/core/util.py\|/opt/eva/app/core/profile.py" && units+=(eva-body)
echo "$changed" | grep -q "/opt/eva/ui/spike/ui_server.py" && units+=(eva-ui-http)
echo "$rootfs_changed" | grep -q "hdmi-watch\|eva-hdmi" && units+=(eva-hdmi)
echo "$changed $rootfs_changed" | grep -q "/opt/eva/ui/spike/\|eva-ui" && ui_reload=1 || ui_reload=0
echo "$rootfs_changed" | grep -q "station-mics-init\|station-audio-init" && echo "note: audio init changed - applied at the next boot"
if [ ${#units[@]} -gt 0 ]; then
    u=$(printf '%s\n' "${units[@]}" | sort -u | tr '\n' ' ')
    echo "restarting: $u"
    "${SSH[@]}" "systemctl restart $u"
fi
# the TV page: reloaded without blocking; with the TV off eva-ui is skipped (its ExecCondition)
[ "$ui_reload" = 1 ] && "${SSH[@]}" "systemctl restart --no-block eva-ui" && echo "TV page reloading"
"${SSH[@]}" "systemctl is-active -q eva-hdmi || systemctl start eva-hdmi"
sleep 5
"${SSH[@]}" "systemctl is-active eva-body eva-ear eva-logship eva-ui-http eva-hdmi | paste -sd' '; \
echo \"TV: \$(cat /sys/class/drm/card0-HDMI-A-1/status), eva-ui \$(systemctl is-active eva-ui)\"; cat /opt/eva/DEPLOYED"
