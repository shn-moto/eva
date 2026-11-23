#!/usr/bin/env python3
"""Pack shim + DTB + Image + initramfs into an AOSP v0 boot image for the
Yandex Station 1 vendor U-Boot ("boota").

Layout of the "kernel" part (loaded and run in place at 0x40080000), as the
upstream shim (github.com/MyszkinPL/yandex-station-linux, boot/shim.S) expects:

    0x000000  shim.bin   watchdog, display routing, Wi-Fi VDDIO, DTB move
    0x010000  DTB        /chosen linux,initrd-start/-end point at the initramfs
    0x180000  Image      arm64, text_offset 0
    after Image's image_size (BSS included), 64 KiB aligned: initramfs

The header ramdisk is a tiny empty cpio placed in the shim log page so boota's
copy can never land on anything in use.

    pack_boot.py --shim shim.bin --image Image --dtb station.dtb \
        --initramfs initramfs.cpio.gz --slot boot -o boot.img
"""
import argparse
import gzip
import hashlib
import struct

LOAD = 0x40080000
DTB_OFF = 0x10000
IMAGE_OFF = 0x180000
ATF = 0x48000000
SHIMLOG_RAMDISK = 0x4E400800
MAX_IMAGE = {"recovery": 32 << 20, "boot": 16 << 20}
MARKER = b"STATIONSHIMLIT01"
PAGE = 2048


# ---------------- flattened device tree: set properties of one node ----------
FDT_MAGIC = 0xD00DFEED
BEGIN, END_NODE, PROP, NOP, END = 1, 2, 3, 4, 9


def _al4(n):
    return (n + 3) & ~3


def fdt_set_props(blob, path, props):
    """Return a new DTB with `props` (name -> bytes) set on node `path`."""
    (magic, _total, off_struct, off_strings, off_rsv, version, last_comp,
     cpu, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
    assert magic == FDT_MAGIC and version >= 17, "not a v17 FDT"
    strings = bytearray(blob[off_strings:off_strings + size_strings])
    rsv_end = off_rsv
    while struct.unpack_from(">QQ", blob, rsv_end) != (0, 0):
        rsv_end += 16
    rsv = blob[off_rsv:rsv_end + 16]

    def name_off(name):
        key = name.encode() + b"\0"
        i = strings.find(key)
        while i > 0 and strings[i - 1] != 0:
            i = strings.find(key, i + 1)
        if i < 0:
            i = len(strings)
            strings.extend(key)
        return i

    def str_at(off):
        return strings[off:strings.index(b"\0", off)].decode()

    want = [p for p in path.strip("/").split("/") if p]
    out = bytearray()
    stack, done, i = [], False, off_struct
    end = off_struct + size_struct
    while i < end:
        tok = struct.unpack_from(">I", blob, i)[0]
        if tok == BEGIN:
            j = blob.index(b"\0", i + 4)
            name = blob[i + 4:j].decode()
            nxt = _al4(j + 1)
            out += blob[i:nxt]
            stack.append(name)  # the root node has an empty name
            i = nxt
            # properties must come before any subnode (libfdt and the kernel's
            # early scan stop looking at the first child): new ones go first
            if [s for s in stack if s] == want and not done:
                for k, v in props.items():
                    out += struct.pack(">III", PROP, len(v), name_off(k)) + v
                    out += b"\0" * (_al4(len(v)) - len(v))
                done = True
            continue
        if tok == PROP:
            ln, no = struct.unpack_from(">II", blob, i + 4)
            nxt = _al4(i + 12 + ln)
            node = [s for s in stack if s]
            if node == want and str_at(no) in props:
                i = nxt  # dropped here, written again at the node's end
                continue
            out += blob[i:nxt]
            i = nxt
            continue
        if tok == END_NODE:
            stack.pop()
            out += blob[i:i + 4]
            i += 4
            continue
        if tok == NOP:
            i += 4
            continue
        if tok == END:
            out += blob[i:i + 4]
            break
        raise ValueError(f"bad FDT token {tok:#x} at {i:#x}")
    if not done:
        raise ValueError(f"node {path} not found")
    hdr_len = 40
    o_rsv = _al4(hdr_len)
    o_struct = o_rsv + len(rsv)
    o_strings = o_struct + len(out)
    total = o_strings + len(strings)
    hdr = struct.pack(">10I", FDT_MAGIC, total, o_struct, o_strings, o_rsv, version, last_comp,
                      cpu, len(strings), len(out))
    return hdr + b"\0" * (o_rsv - hdr_len) + rsv + bytes(out) + bytes(strings)


def fdt_get_prop(blob, path, prop):
    _, _, off_struct, off_strings, *_ = struct.unpack_from(">10I", blob, 0)
    want = [p for p in path.strip("/").split("/") if p]
    stack, i = [], off_struct
    while True:
        tok = struct.unpack_from(">I", blob, i)[0]
        if tok == BEGIN:
            j = blob.index(b"\0", i + 4)
            stack.append(blob[i + 4:j].decode())
            i = _al4(j + 1)
        elif tok == PROP:
            ln, no = struct.unpack_from(">II", blob, i + 4)
            s = off_strings + no
            name = blob[s:blob.index(b"\0", s)].decode()
            if [x for x in stack if x] == want and name == prop:
                return blob[i + 12:i + 12 + ln]
            i = _al4(i + 12 + ln)
        elif tok == END_NODE:
            stack.pop()
            i += 4
        elif tok == NOP:
            i += 4
        else:
            return None


# ---------------- newc cpio / AOSP boot image --------------------------------
def cpio_empty():
    def entry(name, mode):
        nb = name.encode() + b"\0"
        f = (0, mode, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(nb), 0)
        b = b"070701" + b"".join(b"%08X" % v for v in f) + nb
        return b + b"\0" * ((-len(b)) % 4)
    out = entry("TRAILER!!!", 0)
    return out + b"\0" * ((-len(out)) % 512)


HDR = "<8s10I16s512s32s1024s"


def bootimg(kernel, ramdisk):
    pad = lambda b: b + b"\0" * ((-len(b)) % PAGE)
    sha = hashlib.sha1()
    for part in (kernel, ramdisk, b""):
        sha.update(part)
        sha.update(struct.pack("<I", len(part)))
    hdr = struct.pack(HDR, b"ANDROID!", len(kernel), LOAD, len(ramdisk), SHIMLOG_RAMDISK,
                      0, 0x40F00000, 0x40000100, PAGE, 0, 0, b"", b"", sha.digest().ljust(32, b"\0"), b"")
    return pad(hdr) + pad(kernel) + pad(ramdisk)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shim", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--dtb", required=True)
    ap.add_argument("--initramfs", required=True, help="gzip'ed newc cpio")
    ap.add_argument("--slot", choices=("recovery", "boot"), default="boot")
    ap.add_argument("--bootargs", help="kernel command line instead of the DTB's own")
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args()

    shim = bytearray(open(a.shim, "rb").read())
    img = open(a.image, "rb").read()
    dtb = open(a.dtb, "rb").read()
    initrd = open(a.initramfs, "rb").read()
    assert initrd[:2] == b"\x1f\x8b", "initramfs must be gzip'ed"

    _, _, toff, isz, _ = struct.unpack_from("<IIQQQ", img, 0)
    assert img[0x38:0x3C] == b"ARM\x64" and toff == 0, "not an arm64 Image with text_offset 0"
    assert shim[0x38:0x3C] == b"ARM\x64" and len(shim) <= DTB_OFF, "shim.bin"
    lit = shim.find(MARKER)
    assert lit > 0 and shim.find(MARKER, lit + 1) < 0, "shim literals marker"
    lit += len(MARKER)
    assert struct.unpack_from("<Q", shim, lit + 8)[0] == IMAGE_OFF, "shim image_off"

    initrd_off = (IMAGE_OFF + max(isz, len(img)) + 0xFFFF) & ~0xFFFF
    start = LOAD + initrd_off
    chosen = {
        "linux,initrd-start": struct.pack(">Q", start),
        "linux,initrd-end": struct.pack(">Q", start + len(initrd)),
    }
    if a.bootargs:
        # the watchdog must survive a kexec/reboot hand-over (shim + init rely on it)
        assert "watchdog.stop_on_reboot=0" in a.bootargs and "rdinit=/init" in a.bootargs
        chosen["bootargs"] = a.bootargs.encode() + b"\0"
    dtb = fdt_set_props(dtb, "/chosen", chosen)
    assert fdt_get_prop(dtb, "/chosen", "linux,initrd-start") == struct.pack(">Q", start)
    assert fdt_get_prop(dtb, "/chosen", "bootargs"), "bootargs lost"
    dtb += b"\0" * ((-len(dtb)) % 16)
    assert DTB_OFF + len(dtb) <= IMAGE_OFF, "DTB too large"
    img16 = img + b"\0" * ((-len(img)) % 16)

    struct.pack_into("<QQQQ", shim, lit, len(dtb), IMAGE_OFF, len(img16), int(a.slot == "boot"))
    pay = bytearray(shim.ljust(DTB_OFF, b"\0"))
    pay += dtb.ljust(IMAGE_OFF - DTB_OFF, b"\0")
    pay += img16
    pay += b"\0" * (initrd_off - len(pay)) + initrd
    pay += b"\0" * ((-len(pay)) % 16)
    struct.pack_into("<Q", pay, 16, len(pay))  # shim header image_size

    assert LOAD + IMAGE_OFF + isz < ATF and LOAD + len(pay) + 4096 < ATF, "would reach ATF"
    out = bootimg(bytes(pay), gzip.compress(cpio_empty(), 9, mtime=0))
    assert len(out) <= MAX_IMAGE[a.slot], f"image {len(out)} too large for {a.slot}"
    open(a.output, "wb").write(out)
    print(f"OK {a.output}: {len(out)} bytes, initramfs {start:#x}+{len(initrd):#x}, slot {a.slot}")


if __name__ == "__main__":
    main()
