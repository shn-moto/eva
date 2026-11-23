# Eva OS — own system for the Yandex Station 1

Clean-slate Debian (trixie, arm64, systemd) for the Station, built to use the
hardware fully: Mali-400 GPU (lima + Mesa, GLES2), HDMI (sun4i-drm KMS),
cedrus video decoder, the 5-mic array and the TAS5751M amplifiers.

## Boot chain (stage 1)

```
boot0 / vendor U-Boot (eMMC sectors 16..34789)  NEVER TOUCHED
   │  boota from the boot partition (p6)
   ▼
p6  boot      shim + DTB + Linux 7.3.3-station + OUR initramfs   (written by us)
   │  shim counts boots that did not come up (RTC GP7); after 3 it boots recovery
   ▼
p15 rootfs    our Debian (ext4)  ── switch_root, systemd is PID 1
                 boot-ok.service clears the boot word once network + SSH are up

fallback after 3 failed boots:
p10 recovery  upstream debian-v8 image (mainline + busybox init) ── rootfs on p11
              = the current "plain Debian", kept untouched as the rescue system
```

Kernel, DTB, shim and modules: release `debian-v9` of
github.com/MyszkinPL/yandex-station-linux (kernel 7.3.3-station). Own kernel
build comes later, only if we need options the release lacks (snd-aloop, btrfs).

## Partitions (sectors start/size; found by position, not number)

| part | start | size | was | stage 1 | later |
|---|---|---|---|---|---|
| p6  | 172032   | 32768   | boot (Android, rooted)     | **our boot image** | |
| p10 | 4497408  | 65536   | recovery (upstream Debian) | rescue, untouched | our rescue image |
| p11 | 4562944  | 3145728 | Android cache, upstream Debian rootfs | rescue rootfs, untouched | data |
| p15 | 7775232  | 4194304 | Android system slot B      | **our rootfs (ext4)** | rootfs slot A |
| p7  | 204800   | 4194304 | Android system slot A      | untouched (firmware source) | rootfs slot B |
| p1  | 12559356 | 2710532 | Android userdata           | untouched | data |
| p9  | 4464640  | 32768   | misc (upstream boot log)   | untouched | |
| rest|          |         | vendor (env, keys, logos)  | untouched | untouched |

Full eMMC backup (all service partitions, raw head with boot0/U-Boot, Android
system and userdata): `station1/backups/emmc-20261118/`.

## Vendor firmware

Wi-Fi/BT (BCM4356) and amplifier register scripts are Yandex files from the
Station's Android `system` (p7, `/vendor/modules`, `/vendor/firmware`). They are
copied into our rootfs (`/lib/firmware`) from the device itself; they are not
in this repository.

## Status 2026-11-18: stage 1 done

Eva OS boots straight from p6 (U-Boot -> shim -> our initramfs -> p15 ->
systemd), multi-user in ~11 s, ~80 MB RAM used. Verified on the device: Wi-Fi
(5 GHz), SSH (dropbear, same host key), shim boot word cleared by
`eva-boot-ok`, systemd owns the watchdog, GPU (`eglinfo`: GBM, Mesa 25.0.7,
"OpenGL ES 2.0, renderer Mali400"), cedrus loaded, 5-channel mic capture, both
amplifiers (beep played), LED ring (`/sys/class/leds/rgb:indicator-*`),
`gpio-keys` + `rotary-encoder`, Bluetooth `hci0`, zram swap.
The 3-failed-boots fallback to recovery was exercised for real (see below).

Build: `rootfs/build-on-station.sh` on the Station (from the rescue system),
then `tools/mk_initramfs.py` + `tools/pack_boot.py` on the workstation,
`tools/check_pack.py` proves the packer reproduces the upstream release image
byte for byte. Write p6 with `dd`, check by reading back.

## Traps found (do not repeat)

- **misc BCB `boot-recovery`**: Android's bootloader message in the first
  bytes of misc (p9) made U-Boot boot recovery every time, ignoring p6. Its
  32-byte command field was zeroed (2026-11-18); it must stay empty.
- **FDT property order**: `/chosen` has subnodes; properties added after them
  are invisible to the kernel (no initramfs -> panic -> 3 resets -> recovery).
  `pack_boot.py` now puts new properties right after the node opens.
- **tar over a usrmerge root**: unpacking `lib/modules` replaced the `/lib ->
  usr/lib` symlink with a directory (no dynamic loader). Use
  `--keep-directory-symlink`.
- The rescue system's plain `reboot` does nothing: use
  `/usr/local/sbin/station-reboot` there (watchdog reset).

## Screen policy

The TV (HDMI) shows only the Eva UI: no text consoles, no boot text. Kernel
command line is in `bootargs` (packed into the DTB by `pack_boot.py
--bootargs`): `fbcon=vc:2-63` leaves tty1 undrawn (1-based!), `loglevel=1`, `quiet`, hidden cursor;
getty@tty1/autovt are masked and logind has `NAutoVTs=0`. Access is SSH only.
