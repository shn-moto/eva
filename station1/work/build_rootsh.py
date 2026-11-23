import gzip, struct, io, hashlib

raw = open('ramdisk.cpio','rb').read()
entries = []
i = 0
while i < len(raw):
    assert raw[i:i+6] == b'070701'
    hdr = raw[i:i+110]
    ns = int(hdr[94:102], 16); fs = int(hdr[54:62], 16)
    name = raw[i+110:i+110+ns-1].decode()
    off = (i+110+ns+3) & ~3
    entries.append([bytearray(hdr), name, raw[off:off+fs]])
    i = (off+fs+3) & ~3
    if name == 'TRAILER!!!':
        break
print('entries:', len(entries))

busy_hdr = None
max_ino = 0
patched = {'prop': False, 'rc': False}
for e in entries:
    hdr, name, data = e
    max_ino = max(max_ino, int(bytes(hdr[6:14]), 16))
    if name == 'sbin/busybox':
        busy_hdr = bytes(hdr)
    elif name == 'default.prop':
        t = data.decode()
        assert 'ro.secure=1' in t
        e[2] = t.replace('ro.secure=1', 'ro.secure=0').encode()
        patched['prop'] = True
    elif name == 'init.rc':
        t = data.decode()
        assert 'rootshell' not in t
        svc = ("\n# root telnetd for initial setup (temporary)\n"
               "service rootshell /sbin/busybox telnetd -F -p 2323 -l /sbin/rsh\n"
               "    user root\n"
               "    group root\n")
        e[2] = (t.rstrip('\n') + '\n' + svc).encode()
        patched['rc'] = True

assert busy_hdr is not None
assert patched == {'prop': True, 'rc': True}, patched

# новый файл sbin/rsh перед TRAILER!!!
rsh = b'#!/sbin/busybox sh\nexec /sbin/busybox sh\n'
h = bytearray(busy_hdr)
h[6:14]  = ('%08x' % (max_ino + 1)).encode()
h[14:22] = ('%08x' % 0o100755).encode()
trailer_idx = next(idx for idx, e in enumerate(entries) if e[1] == 'TRAILER!!!')
entries.insert(trailer_idx, [h, 'sbin/rsh', rsh])

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

orig = open('../boot-root.img','rb').read()
kernel_size = struct.unpack('<I', orig[8:12])[0]
page_size = struct.unpack('<I', orig[36:40])[0]
def pages(n): return (n + page_size - 1)//page_size
kernel = orig[page_size:page_size+kernel_size]
hdr0 = bytearray(orig[:page_size])
struct.pack_into('<I', hdr0, 16, len(gz))
img = io.BytesIO()
img.write(bytes(hdr0)); img.write(kernel)
img.write(b'\0' * (pages(kernel_size)*page_size - kernel_size))
img.write(gz)
img.write(b'\0' * (pages(len(gz))*page_size - len(gz)))
blob = img.getvalue()
assert len(blob) <= 16777216, len(blob)
blob += b'\0' * (16777216 - len(blob))
open('../boot-rootsh.img','wb').write(blob)
open('ramdisk-rootsh.gz','wb').write(gz)
print('built ../boot-rootsh.img  ramdisk.gz =', len(gz))
print('SHA256:', hashlib.sha256(blob).hexdigest())
