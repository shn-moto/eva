#!/usr/bin/env python3
"""Build Eva OS's initramfs (gzip'ed newc cpio) from a static busybox and our init.

    mk_initramfs.py --busybox busybox --init ../initramfs/init -o initramfs.cpio.gz

The busybox comes from Debian's busybox-static (arm64), see deb_extract.py.
"""
import argparse
import gzip

DIRS = ["dev", "bin", "proc", "sys", "run", "tmp", "newroot", "etc"]


def entry(name, mode, data=b"", ino=0, rdev=(0, 0), nlink=1):
    nb = name.encode() + b"\0"
    f = (ino, mode, 0, 0, nlink, 0, len(data), 0, 0, rdev[0], rdev[1], len(nb), 0)
    b = b"070701" + b"".join(b"%08X" % v for v in f) + nb
    b += b"\0" * ((-len(b)) % 4) + data
    return b + b"\0" * ((-len(b)) % 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--busybox", required=True)
    ap.add_argument("--init", required=True)
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args()
    bb = open(a.busybox, "rb").read()
    assert bb[:4] == b"\x7fELF" and bb[18] == 0xB7, "busybox must be an arm64 ELF"
    init = open(a.init, "rb").read()
    assert init.startswith(b"#!/bin/busybox sh") and b"\r" not in init, "init: LF, busybox shebang"
    out, ino = bytearray(), 100
    for d in DIRS:
        ino += 1
        out += entry(d, 0o040755 if d != "tmp" else 0o041777, ino=ino, nlink=2)
    ino += 1
    out += entry("dev/console", 0o020600, ino=ino, rdev=(5, 1))
    ino += 1
    out += entry("dev/null", 0o020666, ino=ino, rdev=(1, 3))
    ino += 1
    out += entry("bin/busybox", 0o100755, bb, ino=ino)
    ino += 1
    out += entry("bin/sh", 0o120777, b"busybox", ino=ino)
    ino += 1
    out += entry("init", 0o100755, init, ino=ino)
    out += entry("TRAILER!!!", 0)
    out += b"\0" * ((-len(out)) % 512)
    open(a.output, "wb").write(gzip.compress(bytes(out), 9, mtime=0))
    print(f"OK {a.output}: {len(out)} bytes uncompressed")


if __name__ == "__main__":
    main()
