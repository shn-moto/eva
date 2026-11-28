#!/bin/sh
# The eyes' host part, run by deploy/linux.sh eyes: the GoPro link watchdog (gopro_link.sh) copied out
# of the deployed source and run every minute from the user's crontab.
set -e
D="$HOME/eva-data/eyes-host"
mkdir -p "$D"
cp "$HOME/eva-src/eyes/gopro_link.sh" "$D/"
# `|| true`: with no crontab yet grep finds nothing and set -e would end the subshell (print, 2026-11-28)
(crontab -l 2>/dev/null | grep -v "eyes-host/gopro_link.sh" || true; echo "* * * * * sh $D/gopro_link.sh") | crontab -
echo "gopro link watchdog installed"
