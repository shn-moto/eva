#!/usr/bin/env python3
"""Office files -> PDF with the host's own LibreOffice (no second copy of it in a
container). Standard library only: it runs on the host's system Python.

    POST /convert   body: the file, header X-Name: its name (quoted) -> application/pdf
    GET  /health    -> {"ok", "soffice"}

The host must have LibreOffice (soffice on PATH or SOFFICE=...). Without it the
answer is 503 "LibreOffice на хосте не найден" - nothing is converted.
Listens on 127.0.0.1 only: the print service (services/print, host network) calls it.

    python3 convert_host.py            (PORT=8910, SOFFICE=soffice)

Started by the user's crontab (@reboot, and a watchdog every 5 minutes), set by
deploy/linux.sh print.
"""
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse

PORT = int(os.environ.get("PORT", "8910"))
SOFFICE = os.environ.get("SOFFICE") or shutil.which("soffice") or shutil.which("libreoffice")
LOCK = threading.Lock()                    # one LibreOffice at a time


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": bool(SOFFICE), "soffice": SOFFICE})
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path != "/convert":
            return self._send(404, {"detail": "not found"})
        if not SOFFICE:
            return self._send(503, {"detail": "LibreOffice на хосте не найден - офисные файлы не конвертирую"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 50 * 1024 * 1024:
            return self._send(400, {"detail": "пустой файл или больше 50 МБ"})
        data = self.rfile.read(n)
        name = os.path.basename(urllib.parse.unquote(self.headers.get("X-Name") or "file.odt"))
        ext = os.path.splitext(name)[1].lower() or ".odt"
        with tempfile.TemporaryDirectory() as d, LOCK:
            src = os.path.join(d, "in" + ext)
            with open(src, "wb") as f:
                f.write(data)
            try:
                r = subprocess.run([SOFFICE, f"-env:UserInstallation=file://{d}/profile", "--headless",
                                    "--norestore", "--convert-to", "pdf", "--outdir", d, src],
                                   capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                return self._send(504, {"detail": "LibreOffice не успел за 3 минуты"})
            pdf = os.path.join(d, "in.pdf")
            if r.returncode != 0 or not os.path.exists(pdf):
                return self._send(422, {"detail": f"не перевёлся в PDF: {(r.stdout + r.stderr)[-300:]}"})
            self._send(200, open(pdf, "rb").read(), "application/pdf")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"convert_host on 127.0.0.1:{PORT}, soffice: {SOFFICE or 'NOT FOUND'}", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
