#!/bin/sh
# The office converter on the host (convert_host.py: `soffice --headless --convert-to pdf`,
# the host's own LibreOffice). Run by deploy/linux.sh print after the container:
# copies it out of the deployed source, keeps it alive from the user's crontab
# (@reboot + a check every 5 minutes; no sudo), restarts it now.
set -e
D="$HOME/eva-data/print-host"
mkdir -p "$D"
cp "$HOME/eva-src/print/convert_host.py" "$D/"
cat > "$D/run.sh" <<EOF
#!/bin/sh
cd "$D"
curl -sf -m 3 http://127.0.0.1:8910/health >/dev/null || nohup python3 convert_host.py >> convert.log 2>&1 &
EOF
# `|| true`: with no crontab yet grep finds nothing, exits 1, and set -e ended the subshell before
# the two lines - an empty crontab was installed and the converter had no keepalive (2026-11-28)
(crontab -l 2>/dev/null | grep -v "print-host/run.sh" || true; echo "@reboot sh $D/run.sh"; echo "*/5 * * * * sh $D/run.sh") | crontab -
pkill -f "python3 convert_host.py" || true
sleep 1
sh "$D/run.sh"
sleep 2
curl -s -m 3 http://127.0.0.1:8910/health
echo
