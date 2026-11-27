#!/bin/sh
# The TV comes and goes (switched off, unplugged): start the UI when the HDMI
# screen appears, stop it when it goes - cog rendering WebGL for no screen only
# burns the Station's 4 weak cores. Acts on changes only, so a hand-stopped UI
# stays stopped until the TV is plugged in again. A grep every 3 s costs nothing
# (sysfs status gives no inotify; this kernel's hotplug uevents were not checked).
STATUS=/sys/class/drm/card0-HDMI-A-1/status
state() { grep -qx connected "$STATUS" 2>/dev/null && echo on || echo off; }

last=$(state)
[ "$last" = on ] && systemctl start --no-block eva-ui
echo "hdmi: $last"
while sleep 3; do
	now=$(state)
	[ "$now" = "$last" ] && continue
	echo "hdmi: $now"
	if [ "$now" = on ]; then systemctl start --no-block eva-ui; else systemctl stop --no-block eva-ui; fi
	last=$now
done
