#!/bin/sh
# wifi-prefer: keep the Station on the preferred (5 GHz) network.
# If connected elsewhere, switch; on failure re-enable everything (fallback).
PREF=shn5g
IF=wlan0
echo ">> wifi-prefer: watching, prefer $PREF" > /dev/console
while sleep 45; do
    cur=$(wpa_cli -i $IF status 2>/dev/null | sed -n 's/^ssid=//p')
    [ "$cur" = "$PREF" ] && continue
    id=$(wpa_cli -i $IF list_networks 2>/dev/null | awk -v s="$PREF" '$2==s {print $1; exit}')
    [ -z "$id" ] && continue
    wpa_cli -i $IF select_network "$id" >/dev/null 2>&1
    sleep 20
    if wpa_cli -i $IF status 2>/dev/null | grep -q '^wpa_state=COMPLETED'; then
        new=$(wpa_cli -i $IF status 2>/dev/null | sed -n 's/^ssid=//p')
        if [ "$new" = "$PREF" ]; then
            ip=$(ip -4 -o addr show $IF | awk '{print $4}')
            echo ">> wifi-prefer: associated with $PREF (5GHz) $ip" > /dev/console
        fi
    else
        wpa_cli -i $IF enable_network all >/dev/null 2>&1
        echo ">> wifi-prefer: $PREF unreachable, fallback restored" > /dev/console
    fi
done
