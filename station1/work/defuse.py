#!/usr/bin/env python3
"""Defuse the Yandex OTA on the Station the moment Android's telnet appears."""
import base64
import sys
import time

sys.path.insert(0, r"D:\PRG\Pyton\new_alisa\station1\work")
import rootsh  # noqa: E402

FREEZE_SCRIPT = r"""#!/sbin/busybox sh
BB=/sbin/busybox
echo "$($BB date) freeze started pid $$" > /data/local/tmp/freeze.log
while true; do
    for p in /proc/[0-9]*; do
        case "$($BB tr '\0' ' ' < $p/cmdline 2>/dev/null)" in
            maind\ --updatesd*|*/maind\ --updatesd*)
                [ "$($BB awk '/^State/{print $2}' $p/status)" = T ] ||
                    { kill -STOP ${p#/proc/} && echo "$($BB date) froze ${p#/proc/}" >> /data/local/tmp/freeze.log; } ;;
        esac
    done
    $BB sleep 3
done
"""


def sh(cmd, timeout=30):
    out = rootsh.run(cmd, timeout)
    print(f"$ {cmd[:90]}\n{out}\n", flush=True)
    return out


def main():
    print("жду telnet 192.168.1.20:2323 ...", flush=True)
    while True:
        try:
            rootsh.run("echo PING", 5)
            break
        except Exception:
            time.sleep(2)
    print("== ANDROID WINDOW OPEN, обезвреживаю ==", flush=True)

    sh("rm -f /cache/update_*.zip /cache/recovery/command /cache/recovery/last_command; "
       "ls -la /cache/ | grep -i update; ls -la /cache/recovery/ 2>/dev/null; echo DEFUSED")

    b64 = base64.b64encode(FREEZE_SCRIPT.encode()).decode()
    sh(f"echo {b64} | /sbin/busybox base64 -d > /data/local/tmp/freeze-updatesd.sh "
       "&& chmod 755 /data/local/tmp/freeze-updatesd.sh && head -2 /data/local/tmp/freeze-updatesd.sh")

    sh("nohup /sbin/busybox sh /data/local/tmp/freeze-updatesd.sh >/dev/null 2>&1 & "
       "sleep 5; cat /data/local/tmp/freeze.log")

    sh("for p in /proc/[0-9]*; do c=$(cat $p/cmdline 2>/dev/null | tr '\\0' ' '); "
       "case \"$c\" in *updatesd*) echo \"$p: $c [$(awk '/^State/{print $2}' $p/status)]\";; esac; done")

    print("== ГОТОВО. Не выключай колонку — скажи мне, что видишь. ==", flush=True)


if __name__ == "__main__":
    main()
