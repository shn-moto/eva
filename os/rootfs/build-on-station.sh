#!/bin/sh
# Build Eva OS's root filesystem natively on the Station, from the running
# rescue Debian, onto the partition at sector 7775232 (Android's former system
# slot B, p15). Formats that partition: everything on it is lost.
#
#   sh build-on-station.sh /tmp/eva-in      (dir with modules.tar.gz, files/)
#
# Input dir (copied over from the workstation):
#   modules.tar.gz   lib/modules/<release> from the kernel release
#   files/           tree copied over the new root (units, configs, scripts)
set -eu

IN=${1:?usage: build-on-station.sh <input dir>}
ROOT_START=7775232 ROOT_SECTORS=4194304
SYS_START=204800 SYS_SECTORS=4194304          # Android system slot A: firmware source
MNT=/mnt/eva
SUITE=trixie
MIRROR=http://deb.debian.org/debian
T=/tmp/eva-build

say() { echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

part_at() {
	n=""; c=0
	for d in /sys/class/block/mmcblk0p*; do
		[ "$(cat $d/start)" = "$1" ] && [ "$(cat $d/size)" = "$2" ] && { n=${d##*/}; c=$((c+1)); }
	done
	[ $c = 1 ] && echo /dev/$n
}

DEV=$(part_at $ROOT_START $ROOT_SECTORS) || die "no partition at $ROOT_START/$ROOT_SECTORS"
SYS=$(part_at $SYS_START $SYS_SECTORS) || die "no Android system partition"
grep -q "^$DEV " /proc/mounts && die "$DEV is mounted"
[ -s "$IN/modules.tar.gz" ] && [ -d "$IN/files" ] || die "input dir incomplete"

# --- tools: debootstrap into RAM (the rescue root has no room) ---
say "debootstrap"
mkdir -p $T/lists/partial $T/cache/archives/partial $T/tools
O="-o Dir::State::Lists=$T/lists -o Dir::Cache=$T/cache -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache="
apt-get $O update -qq
(cd $T/cache/archives && apt-get $O download debootstrap wget)
for d in $T/cache/archives/*.deb; do dpkg -x $d $T/tools; done
rm -rf $T/lists $T/cache
export DEBOOTSTRAP_DIR=$T/tools/usr/share/debootstrap
export PATH=$T/tools/usr/bin:$PATH
wget --version | head -1

# --- the partition ---
say "formatting $DEV (ext4, label eva-root)"
mkfs.ext4 -q -F -L eva-root -O ^metadata_csum_seed $DEV
mkdir -p $MNT
mount -o noatime $DEV $MNT

PKGS=$(grep -v '^#' "$IN/packages" | tr -s ' \n' ',' | sed 's/^,//; s/,$//')
say "debootstrap $SUITE (this takes a while)"
$T/tools/usr/sbin/debootstrap --variant=minbase --include="$PKGS" $SUITE $MNT $MIRROR

# --- kernel modules ---
say "kernel modules"
tar xzf "$IN/modules.tar.gz" -C $MNT --keep-directory-symlink
[ -L $MNT/lib ] || die "lib symlink lost"
KREL=$(ls $MNT/lib/modules | head -1)

# --- vendor firmware from Android's own system partition (read-only) ---
say "firmware from $SYS"
mkdir -p /mnt/sysa $MNT/lib/firmware/brcm
blockdev --setro $SYS
mount -t ext4 -o ro,noload $SYS /mnt/sysa
while read -r dst src; do
	cp /mnt/sysa/$src $MNT/lib/firmware/$dst
done <<-FW
brcm/brcmfmac4356-sdio.bin vendor/modules/fw_bcm4356a2_ag.bin
brcm/brcmfmac4356-sdio.txt vendor/modules/nvram.txt
brcm/BCM4356A2.hcd vendor/modules/BCM4354A2.hcd
tas5751m-2a.txt vendor/firmware/tas5751m-2a.txt
tas5751m-2b.txt vendor/firmware/tas5751m-2b.txt
FW
umount /mnt/sysa
blockdev --setrw $SYS
(cd $MNT/lib/firmware && sha256sum brcm/* tas5751m-*.txt)

# --- our files, then the owner's secrets from the running rescue system ---
say "configuration"
cp -a "$IN/files/." $MNT/
mkdir -p $MNT/etc/wpa_supplicant $MNT/root/.ssh $MNT/etc/dropbear
cp /etc/wpa-station.conf $MNT/etc/wpa_supplicant/wpa_supplicant-wlan0.conf
sed -i 's|^ctrl_interface=.*|ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev|' \
	$MNT/etc/wpa_supplicant/wpa_supplicant-wlan0.conf
chmod 600 $MNT/etc/wpa_supplicant/wpa_supplicant-wlan0.conf
cp /root/.ssh/authorized_keys $MNT/root/.ssh/ && chmod 700 $MNT/root/.ssh && chmod 600 $MNT/root/.ssh/authorized_keys
cp -a /etc/dropbear/. $MNT/etc/dropbear/ 2>/dev/null || true
echo eva > $MNT/etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\teva\n' > $MNT/etc/hosts
printf 'LABEL=eva-root / ext4 noatime,errors=remount-ro 0 1\n' > $MNT/etc/fstab
ln -sf /usr/share/zoneinfo/Europe/Warsaw $MNT/etc/localtime
echo Europe/Warsaw > $MNT/etc/timezone

cat > $MNT/etc/apt/sources.list <<EOF
deb $MIRROR $SUITE main contrib non-free non-free-firmware
deb $MIRROR $SUITE-updates main contrib non-free non-free-firmware
deb http://deb.debian.org/debian-security $SUITE-security main contrib non-free non-free-firmware
EOF

# --- inside: depmod, services ---
for m in proc sys dev dev/pts; do mount --bind /$m $MNT/$m; done
chroot $MNT /bin/sh -eu <<EOF
depmod $KREL
passwd -l root
for u in systemd-networkd systemd-resolved wpa_supplicant@wlan0 dropbear \
	eva-sound eva-boot-ok.service eva-health.timer; do
	systemctl enable \$u || echo "!! WARN: cannot enable \$u"
done
systemctl disable wpa_supplicant 2>/dev/null || true
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
apt-get clean
EOF
for m in dev/pts dev sys proc; do umount $MNT/$m; done

echo "eva-root $(date -u +%Y-%m-%dT%H:%MZ) kernel $KREL" > $MNT/etc/eva-release
df -h $MNT | tail -1
sync
umount $MNT
say "done: $DEV holds Eva OS ($KREL)"
