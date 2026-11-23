#!/system/bin/sh
# Debian for the Yandex Station 1, release debian-v8, in one command. On the
# Station, in Android, as root:
#   curl -kfsSL https://github.com/MyszkinPL/yandex-station-linux/releases/latest/download/install.sh | sh
# flags go after "sh -s --": ... | sh -s -- --mode direct --key "ssh-ed25519 ..."
F=/data/local/tmp/station-debian.run
echo "Debian для Яндекс Станции 1: скачиваю установщик debian-v8..."
cd /data/local/tmp 2>/dev/null || { echo "Нет /data/local/tmp: это запускается в Android Станции."; exit 3; }
rm -f $F.part
if ! curl -kfsSL --connect-timeout 20 --retry 2 -o $F.part 2>/dev/null "https://github.com/MyszkinPL/yandex-station-linux/releases/download/debian-v8/station-debian.run" ||
	[ "$(wc -c < $F.part | tr -d ' ')" != 2292978 ]; then
	rm -f $F.part
	echo "Установщик не скачался целиком. Проверьте интернет на Станции и повторите."
	exit 5
fi
mv $F.part $F
# the questions need the terminal: stdin is this very script
if [ -t 1 ] && (exec < /dev/tty) 2>/dev/null; then exec sh $F "$@" < /dev/tty; fi
exec sh $F "$@"
