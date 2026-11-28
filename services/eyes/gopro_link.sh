#!/bin/sh
# A GoPro's Wi-Fi link on the hub (NetworkManager connections named gopro*): when the camera restarts
# its access point (its phone app disconnects, a reboot) the hub stays "connected" to the old session
# and the camera no longer answers - only a reconnect brings it back (2026-11-28, HERO11 Black after
# Quik was closed). Run every minute from the user's crontab (install_host.sh); needs sudo for nmcli.
CAM=${GOPRO_IP:-10.5.5.9}
for c in $(nmcli -t -f NAME con show | grep '^gopro'); do
    nmcli -t -f GENERAL.STATE con show "$c" 2>/dev/null | grep -q activated || continue   # not linked: NM retries itself
    curl -sf -m 4 "http://$CAM:8080/gopro/camera/keep_alive" >/dev/null && continue
    sleep 5
    curl -sf -m 4 "http://$CAM:8080/gopro/camera/keep_alive" >/dev/null && continue
    sudo -n nmcli con up "$c" >/dev/null 2>&1 && logger -t eva-gopro "relinked $c: the camera stopped answering"
done
