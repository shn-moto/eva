import gzip, struct, io, hashlib

# --- извлекаем ramdisk из boot-current.img ---
orig = open('boot-current.img', 'rb').read()
kernel_size = struct.unpack('<I', orig[8:12])[0]
page_size = struct.unpack('<I', orig[36:40])[0]
ramdisk_size = struct.unpack('<I', orig[16:20])[0]
pages = lambda n: (n + page_size - 1) // page_size
rd_off = page_size + pages(kernel_size) * page_size
gz_orig = orig[rd_off:rd_off + ramdisk_size]
raw = gzip.decompress(gz_orig)

entries = []
i = 0
while i < len(raw):
    assert raw[i:i+6] == b'070701'
    hdr = raw[i:i+110]
    ns = int(hdr[94:102], 16); fs = int(hdr[54:62], 16)
    name = raw[i+110:i+110+ns-1].decode()
    off = (i + 110 + ns + 3) & ~3
    entries.append([bytearray(hdr), name, raw[off:off+fs]])
    i = (off + fs + 3) & ~3
    if name == 'TRAILER!!!':
        break
print('entries:', len(entries))

busy_hdr = None
max_ino = 0
patched = {'rc': False}
for e in entries:
    hdr, name, data = e
    max_ino = max(max_ino, int(bytes(hdr[6:14]), 16))
    if name == 'sbin/busybox':
        busy_hdr = bytes(hdr)
    elif name == 'init.rc':
        t = data.decode()
        assert 'freezer' not in t
        svc = ("\n# keep Yandex OTA daemon frozen (maind --updatesd)\n"
               "service freezer /sbin/freeze-updatesd.sh\n"
               "    user root\n"
               "    group root\n")
        e[2] = (t.rstrip('\n') + '\n' + svc).encode()
        patched['rc'] = True

assert busy_hdr is not None
assert patched['rc'], 'init.rc не пропатчен'

FREEZE = (b'#!/sbin/busybox sh\n'
          b'BB=/sbin/busybox\n'
          b'while true; do\n'
          b'    for p in /proc/[0-9]*; do\n'
          b'        case "$($BB tr \'\\0\' \' \' < $p/cmdline 2>/dev/null)" in\n'
          b'            maind\\ --updatesd*|*/maind\\ --updatesd*)\n'
          b'                [ "$($BB awk \'/^State/{print $2}\' $p/status)" = T ] ||\n'
          b'                    kill -STOP ${p#/proc/} ;;\n'
          b'        esac\n'
          b'    done\n'
          b'    $BB sleep 3\n'
          b'done\n')

h = bytearray(busy_hdr)
h[6:14] = ('%08x' % (max_ino + 1)).encode()
h[14:22] = ('%08x' % 0o100755).encode()
trailer_idx = next(idx for idx, e in enumerate(entries) if e[1] == 'TRAILER!!!')
entries.insert(trailer_idx, [h, 'sbin/freeze-updatesd.sh', FREEZE])

out = io.BytesIO()
for hdr, name, data in entries:
    if name == 'TRAILER!!!':
        data = b''
    nb = name.encode() + b'\0'
    hdr[54:62] = ('%08x' % len(data)).encode()
    hdr[94:102] = ('%08x' % len(nb)).encode()
    out.write(bytes(hdr)); out.write(nb)
    out.write(b'\0' * ((4 - (110 + len(nb)) % 4) % 4))
    out.write(data)
    out.write(b'\0' * ((4 - len(data) % 4) % 4))
cpio = out.getvalue()
cpio += b'\0' * ((512 - len(cpio) % 512) % 512)
gz = gzip.compress(cpio, compresslevel=9, mtime=0)

kernel = orig[page_size:page_size + kernel_size]
hdr0 = bytearray(orig[:page_size])
struct.pack_into('<I', hdr0, 16, len(gz))
img = io.BytesIO()
img.write(bytes(hdr0)); img.write(kernel)
img.write(b'\0' * (pages(kernel_size) * page_size - kernel_size))
img.write(gz)
img.write(b'\0' * (pages(len(gz)) * page_size - len(gz)))
blob = img.getvalue()
assert len(blob) <= 16777216, len(blob)
blob += b'\0' * (16777216 - len(blob))
open('../boot-noupdate.img', 'wb').write(blob)
print('built ../boot-noupdate.img  ramdisk.gz =', len(gz))
print('SHA256:', hashlib.sha256(blob).hexdigest())
