#!/usr/bin/env python3
"""Run a command on the Station's Debian over SSH (key auth, no shell quoting issues).
Usage: py -3 sshd.py "remote command" [timeout]
"""
import subprocess
import sys

KEY = r"D:\PRG\Pyton\new_alisa\station1\debian_ed25519"
HOST = "root@192.168.137.116"


def run(cmd, timeout=30, host=HOST):
    args = [
        "ssh", "-i", KEY, host,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        cmd,
    ]
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "id"
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    host = f"root@{sys.argv[3]}" if len(sys.argv) > 3 else HOST
    rc, out = run(cmd, timeout, host)
    print(out.strip())
    sys.exit(rc)
