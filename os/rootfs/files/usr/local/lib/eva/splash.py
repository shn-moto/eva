#!/usr/bin/env python3
"""Boot logo on the TV straight through DRM (no fbdev on this kernel, no
plymouth): a dumb buffer with the logo centred on black, shown in the same
1280x720 mode the UI uses, so the TV does not re-sync when the UI takes over.

Holds DRM master until SIGUSR1 (eva-ui's ExecStartPre): then it drops master
so cog can take the screen, keeps its buffer a little longer (no black gap)
and exits.

    splash.py [logo.xrgb.z]     (8-byte header: width, height; zlib XRGB8888)
"""
import ctypes
import fcntl
import mmap
import os
import signal
import struct
import sys
import time
import zlib

LOGO = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/eva/logo.xrgb.z"
WANT = (1280, 720)


def IOWR(nr, size):
    return (3 << 30) | (size << 16) | (ord("d") << 8) | nr


def IO(nr):
    return (ord("d") << 8) | nr


MODEINFO = "<I5H5HIII32s"                      # struct drm_mode_modeinfo (68 bytes)
MODE_SZ = struct.calcsize(MODEINFO)
GETRES, GETCONN, GETENC, SETCRTC = 0xA0, 0xA7, 0xA6, 0xA2
ADDFB, CREATE_DUMB, MAP_DUMB, DROP_MASTER = 0xAE, 0xB2, 0xB3, 0x1F


def ioctl(fd, nr, fmt, *vals):
    buf = bytearray(struct.pack(fmt, *vals))
    fcntl.ioctl(fd, IOWR(nr, len(buf)), buf)
    return struct.unpack(fmt, buf)


def arr(n, fmt="I"):
    a = (ctypes.c_uint32 * max(n, 1))() if fmt == "I" else (ctypes.c_uint8 * max(n, 1))()
    return a, ctypes.addressof(a)


def main():
    fd = os.open("/dev/dri/card0", os.O_RDWR | os.O_CLOEXEC)
    # resources: connectors and crtcs
    r = ioctl(fd, GETRES, "<QQQQ8I", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    ncrtc, nconn, nenc = r[5], r[6], r[7]
    crtcs, pcrtc = arr(ncrtc)
    conns, pconn = arr(nconn)
    encs, penc = arr(nenc)
    fbs, pfb = arr(r[4])
    ioctl(fd, GETRES, "<QQQQ8I", pfb, pcrtc, pconn, penc, r[4], ncrtc, nconn, nenc, 0, 0, 0, 0)
    for cid in list(conns)[:nconn]:
        c = ioctl(fd, GETCONN, "<QQQQ12I", 0, 0, 0, 0, 0, 0, 0, 0, cid, 0, 0, 0, 0, 0, 0, 0)
        nmodes, nprops, nencs, connection = c[4], c[5], c[6], c[11]
        if connection != 1 or not nmodes:          # 1 = connected
            continue
        modes = (ctypes.c_uint8 * (MODE_SZ * nmodes))()
        props, pprops = arr(nprops)
        vals = (ctypes.c_uint64 * max(nprops, 1))()
        cencs, pcencs = arr(nencs)
        c = ioctl(fd, GETCONN, "<QQQQ12I", pcencs, ctypes.addressof(modes), pprops,
                  ctypes.addressof(vals), nmodes, nprops, nencs, 0, cid, 0, 0, 0, 0, 0, 0, 0)
        mlist = [struct.unpack_from(MODEINFO, bytes(modes), i * MODE_SZ) for i in range(nmodes)]
        mode = next((m for m in mlist if (m[1], m[6]) == WANT and m[11] == 60), None) or \
            next((m for m in mlist if (m[1], m[6]) == WANT), None) or mlist[0]
        enc_id = c[7] or cencs[0]
        e = ioctl(fd, GETENC, "<5I", enc_id, 0, 0, 0, 0)
        crtc = e[2] or next(crtcs[i] for i in range(ncrtc) if e[3] & (1 << i))
        break
    else:
        sys.exit("splash: no connected display")
    w, h = mode[1], mode[6]
    # dumb buffer, black, logo in the middle
    d = ioctl(fd, CREATE_DUMB, "<4IIIQ", h, w, 32, 0, 0, 0, 0)
    handle, pitch, size = d[4], d[5], d[6]
    fb = ioctl(fd, ADDFB, "<7I", 0, w, h, pitch, 32, 24, handle)[0]
    off = ioctl(fd, MAP_DUMB, "<IIQ", handle, 0, 0)[2]
    mem = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=off)
    mem[:] = bytes(size)
    data = open(LOGO, "rb").read()
    lw, lh = struct.unpack_from("<II", data)
    px = zlib.decompress(data[8:])
    x0, y0 = (w - lw) // 2, (h - lh) // 2
    for y in range(min(lh, h)):
        o = (y0 + y) * pitch + x0 * 4
        mem[o:o + lw * 4] = px[y * lw * 4:(y + 1) * lw * 4]
    one, pone = arr(1)
    one[0] = cid
    ioctl(fd, SETCRTC, "<QIIIIIII" + MODEINFO[1:], pone, 1, crtc, fb, 0, 0, 0, 1, *mode)
    print(f"splash: {w}x{h} on crtc {crtc}, connector {cid}", flush=True)

    done = []
    signal.signal(signal.SIGUSR1, lambda *a: done.append(1))
    signal.signal(signal.SIGTERM, lambda *a: done.append(1))
    while not done:
        time.sleep(0.2)
    fcntl.ioctl(fd, IO(DROP_MASTER))
    print("splash: screen handed over", flush=True)
    time.sleep(15)                                 # keep the buffer until the UI has drawn


if __name__ == "__main__":
    main()
