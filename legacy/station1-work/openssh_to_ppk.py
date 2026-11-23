#!/usr/bin/env python3
"""Convert an unencrypted OpenSSH ed25519 private key to PuTTY PPK v2
(compatible with PuTTY >= 0.71, which cannot read the OpenSSH format).

Usage: py -3 openssh_to_ppk.py <openssh_key> <out.ppk> [comment]
"""
import base64
import hashlib
import hmac
import struct
import sys


def read_string(buf, off):
    (n,) = struct.unpack_from(">I", buf, off)
    return buf[off + 4: off + 4 + n], off + 4 + n


def pack_string(b):
    return struct.pack(">I", len(b)) + b


def pack_mpint(b):
    # mpint: big-endian, без ведущих нулей, с префиксом 0x00 если старший бит = 1.
    # PuTTY хранит приватную часть всех ECC-ключей (включая ed25519) как mpint.
    b = b.lstrip(b"\x00") or b"\x00"
    if b[0] & 0x80:
        b = b"\x00" + b
    return struct.pack(">I", len(b)) + b


def parse_openssh_ed25519(path):
    data = open(path, "rb").read()
    # strip PEM-ish armor
    b64 = "".join(
        l for l in data.decode().splitlines()
        if l and not l.startswith("-----"))
    raw = base64.b64decode(b64)
    magic = b"openssh-key-v1\x00"
    assert raw.startswith(magic), "not an openssh-key-v1 file"
    off = len(magic)
    cipher, off = read_string(raw, off)
    kdf, off = read_string(raw, off)
    _, off = read_string(raw, off)  # kdf options
    assert cipher == b"none" and kdf == b"none", "key is encrypted"
    (nkeys,) = struct.unpack_from(">I", raw, off)
    off += 4
    assert nkeys == 1
    pub_blob, off = read_string(raw, off)
    # public blob: string("ssh-ed25519") + string(pub32)
    alg, po = read_string(pub_blob, 0)
    assert alg == b"ssh-ed25519", alg
    pub, po = read_string(pub_blob, po)
    priv_block, off = read_string(raw, off)
    c1, c2 = struct.unpack_from(">II", priv_block, 0)
    assert c1 == c2, "bad checkints"
    poff = 8
    alg2, poff = read_string(priv_block, poff)
    assert alg2 == b"ssh-ed25519"
    pub2, poff = read_string(priv_block, poff)
    priv64, poff = read_string(priv_block, poff)
    comment, poff = read_string(priv_block, poff)
    assert pub2 == pub
    assert len(priv64) == 64
    return pub, priv64, comment.decode(errors="replace")


def write_ppk_v2(pub, priv64, comment, out_path):
    alg = b"ssh-ed25519"
    seed = priv64[:32]  # в PPK для ed25519 хранится только 32-байтовый seed
    pub_blob = pack_string(alg) + pack_string(pub)
    # Приватный блоб PPK для ed25519 — ТОЛЬКО mpint приватного ключа (seed).
    # Алгоритм и публичный ключ внутри не повторяются (см. PuTTY manual, Appendix C.3.4).
    priv_blob = pack_mpint(seed)

    mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest()
    mac_data = (pack_string(alg) + pack_string(b"none")
                + pack_string(comment.encode())
                + pack_string(pub_blob) + pack_string(priv_blob))
    mac = hmac.new(mac_key, mac_data, hashlib.sha1).hexdigest()

    def lines(blob):
        b = base64.b64encode(blob).decode()
        return [b[i:i + 64] for i in range(0, len(b), 64)]

    pub_l = lines(pub_blob)
    priv_l = lines(priv_blob)
    out = [f"PuTTY-User-Key-File-2: ssh-ed25519",
           "Encryption: none",
           f"Comment: {comment}",
           f"Public-Lines: {len(pub_l)}", *pub_l,
           f"Private-Lines: {len(priv_l)}", *priv_l,
           f"Private-MAC: {mac}"]
    with open(out_path, "w", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    pub, priv64, comment = parse_openssh_ed25519(src)
    if len(sys.argv) > 3:
        comment = sys.argv[3]
    write_ppk_v2(pub, priv64, comment, dst)
    print("OK ->", dst)
