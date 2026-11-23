#!/usr/bin/env python3
"""Self-check of pack_boot.py against an upstream release image.

Takes the release's boot-station.img apart, packs the same pieces again with
pack_boot.py and compares: everything but the DTB must be byte-identical, and
the DTB must carry the same /chosen initrd range and bootargs.

    check_pack.py <release dir with boot-station.img> <kernel dir with shim.bin, Image, dtb/station.dtb>
"""
import gzip
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pack_boot as pb  # noqa: E402


def parse(data):
    f = struct.unpack_from(pb.HDR, data)
    ks, rs, ps = f[1], f[3], f[8]
    up = lambda n: (n + ps - 1) // ps * ps
    return data[ps:ps + ks], data[ps + up(ks):ps + up(ks) + rs]


def main():
    rel, kdir = sys.argv[1], sys.argv[2]
    up_img = open(os.path.join(rel, "boot-station.img"), "rb").read()
    pay, _ = parse(up_img)
    dtb_len = struct.unpack_from(">I", pay, pb.DTB_OFF + 4)[0]
    dtb = pay[pb.DTB_OFF:pb.DTB_OFF + dtb_len]
    s = struct.unpack(">Q", pb.fdt_get_prop(dtb, "/chosen", "linux,initrd-start"))[0]
    e = struct.unpack(">Q", pb.fdt_get_prop(dtb, "/chosen", "linux,initrd-end"))[0]
    initrd = pay[s - pb.LOAD:e - pb.LOAD]
    assert initrd[:2] == b"\x1f\x8b"
    gzip.decompress(initrd)
    with tempfile.TemporaryDirectory() as t:
        ird = os.path.join(t, "initrd.gz")
        open(ird, "wb").write(initrd)
        out = os.path.join(t, "boot.img")
        subprocess.run([sys.executable, "-I", os.path.join(HERE, "pack_boot.py"),
                        "--shim", os.path.join(kdir, "shim.bin"), "--image", os.path.join(kdir, "Image"),
                        "--dtb", os.path.join(kdir, "dtb", "station.dtb"), "--initramfs", ird,
                        "--slot", "boot", "-o", out], check=True)
        mine, _ = parse(open(out, "rb").read())
    assert len(mine) == len(pay), f"payload size {len(mine)} != {len(pay)}"
    my_dtb_len = struct.unpack_from(">I", mine, pb.DTB_OFF + 4)[0]
    my_dtb = mine[pb.DTB_OFF:pb.DTB_OFF + my_dtb_len]
    for p in ("linux,initrd-start", "linux,initrd-end", "bootargs"):
        assert pb.fdt_get_prop(my_dtb, "/chosen", p) == pb.fdt_get_prop(dtb, "/chosen", p), p
    # the whole DTB, not only /chosen: property order matters to the kernel
    assert my_dtb == dtb, "DTB differs from the release's"
    assert mine == pay, "payload differs"
    print("pack_boot reproduces the release payload byte for byte")


if __name__ == "__main__":
    main()
