#!/usr/bin/env python3
"""GPU arbiter on the hub: photolib's indexing gets the whole GPU.

The GPU (8 GB) is shared: photolib (CLIP, faces) and Eva's local router. While
photolib indexes, Eva's GPU containers stop (Eva falls back to her CPU gate);
when it is done they start again. Out of indexing photolib frees its own models
when idle (MODEL_IDLE_UNLOAD_MIN), so the router has room.

    PHOTOLIB_URL   photolib API (default http://127.0.0.1:8000), asked GET /admin/gpu/busy
    CONTAINERS     comma separated containers that yield the GPU (default eva-router)
    INTERVAL       seconds between checks (default 20)
    STATE          file with the containers stopped by the arbiter (it starts only those again)
    PORT           status page (GET /health, default 8907)

Docker is driven through its socket (/var/run/docker.sock mounted in).
"""
import http.client
import http.server
import json
import os
import socket
import threading
import time
import urllib.request

PHOTOLIB = os.environ.get("PHOTOLIB_URL", "http://127.0.0.1:8000")
CONTAINERS = [c.strip() for c in os.environ.get("CONTAINERS", "eva-router").split(",") if c.strip()]
INTERVAL = float(os.environ.get("INTERVAL", "20"))
STATE = os.environ.get("STATE", "/state/stopped.json")
PORT = int(os.environ.get("PORT", "8907"))
status = {"indexing": None, "stopped": [], "checked": None, "error": None}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class _Unix(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=60)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(60)
        self.sock.connect("/var/run/docker.sock")


def docker(method, path):
    c = _Unix()
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def running(name):
    code, body = docker("GET", f"/containers/{name}/json")
    return code == 200 and json.loads(body)["State"]["Running"]


def load():
    try:
        return set(json.load(open(STATE)))
    except (OSError, ValueError):
        return set()


def save(names):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(sorted(names), f)
    os.replace(STATE + ".tmp", STATE)


def check(stopped):
    with urllib.request.urlopen(PHOTOLIB + "/admin/gpu/busy", timeout=15) as r:
        busy = json.loads(r.read())
    status.update(indexing=busy.get("indexing"), checked=time.strftime("%H:%M:%S"), error=None)
    if busy.get("indexing"):
        for name in CONTAINERS:
            if name not in stopped and running(name):
                code, _ = docker("POST", f"/containers/{name}/stop?t=10")
                log(f"photolib indexes ({', '.join(busy.get('tasks', []))}): stopped {name} ({code})")
                stopped.add(name)
                save(stopped)
    elif stopped:
        for name in sorted(stopped):
            code, _ = docker("POST", f"/containers/{name}/start")
            log(f"indexing done: started {name} ({code})")
            if code in (204, 304):
                stopped.discard(name)
        save(stopped)
    status["stopped"] = sorted(stopped)


class Status(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(dict(status, ok=True)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    threading.Thread(target=http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Status).serve_forever,
                     daemon=True).start()
    stopped = load()
    log(f"watching photolib for {', '.join(CONTAINERS)}; stopped by me before: {sorted(stopped) or 'none'}")
    while True:
        try:
            check(stopped)
        except Exception as e:          # photolib down for a rebuild: keep things as they are
            status["error"] = str(e)[:200]
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
