#!/usr/bin/env python3
"""Static server for the UI spike, plus POST /snap: the page sends one rendered
frame (a PNG data URL) so the workstation can see what the TV shows.

    python3 spike_server.py [port]      (serves the current directory)
"""
import base64
import http.server
import sys
import time


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/snap":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        head, _, data = body.partition(b",")
        if not head.startswith(b"data:image/png"):
            self.send_error(400)
            return
        import os
        os.makedirs("snaps", exist_ok=True)
        tag = "".join(c for c in self.headers.get("X-Name", "") if c.isalnum() or c in "-_") or "snap"
        name = os.path.join("snaps", f"{tag}-{time.strftime('%H%M%S')}.png")
        with open(name, "wb") as f:
            f.write(base64.b64decode(data))
        self.send_response(204)
        self.end_headers()
        print("snap saved", name, flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
