#!/usr/bin/env python3
"""Extract one file from a Debian .deb (ar + data.tar.*) without running anything.

    deb_extract.py busybox-static_*.deb usr/bin/busybox out/busybox
"""
import io
import lzma
import sys
import tarfile


def members(deb):
    data = open(deb, "rb").read()
    assert data[:8] == b"!<arch>\n", "not an ar archive"
    i = 8
    while i < len(data):
        name = data[i:i + 16].decode().strip().rstrip("/")
        size = int(data[i + 48:i + 58].decode().strip())
        yield name, data[i + 60:i + 60 + size]
        i += 60 + size + (size & 1)


def main():
    deb, want, out = sys.argv[1:4]
    for name, body in members(deb):
        if name.startswith("data.tar"):
            if name.endswith(".xz"):
                body = lzma.decompress(body)
            elif name.endswith(".zst"):
                sys.exit("zstd data member: unsupported here")
            with tarfile.open(fileobj=io.BytesIO(body)) as t:
                for m in t.getmembers():
                    if m.name.lstrip("./") == want and m.isfile():
                        open(out, "wb").write(t.extractfile(m).read())
                        print(f"OK {want} -> {out} ({m.size} bytes)")
                        return
    sys.exit(f"{want} not in {deb}")


if __name__ == "__main__":
    main()
