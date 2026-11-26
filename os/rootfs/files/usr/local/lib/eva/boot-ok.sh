#!/bin/sh
# This boot came up (network and SSH): tell the shim. Started from the boot
# partition, the shim counts boots in the RTC boot word and after three that
# never got here boots recovery (the rescue system). Clearing it is only ours
# to do when the shim says we were started from boot (its log page, slot 1).
set -eu
SHIM=0x4E400000 BOOTW=0x01F0011C BOOTW_OK=0x53540000
dm() { busybox devmem "$@"; }
if [ "$(dm $SHIM 32)" != 0x324D4853 ]; then
	echo "no shim log: nothing to clear"; exit 0
fi
if [ "$(dm $((SHIM + 0x48)) 32)" != 0x00000001 ]; then
	echo "started from recovery: boot word left alone"; exit 0
fi
dm $BOOTW 32 $BOOTW_OK
echo "boot word cleared: $(dm $BOOTW 32)"
