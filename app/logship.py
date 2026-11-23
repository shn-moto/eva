#!/usr/bin/env python3
"""Eva's logs go to shn-linux, not to the Station's disk.

The journal on the Station lives in RAM only (journald Storage=volatile,
24 MB); this service follows it and inserts every entry into Postgres
assistant_memory.eva_log on shn-linux. The root partition was 100 % full on
2026-11-23 (journal 168 MB of 2 GB). The cursor of the last shipped entry is
kept on disk once a minute: after a restart shipping goes on from it, and
`cursor` is unique in the table, so an entry sent twice is stored once.

    journalctl -f -o json  ->  batches every 2 s  ->  INSERT ... ON CONFLICT DO NOTHING
"""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.util import secrets  # noqa: E402

CURSOR_FILE = "/var/lib/eva/logship.cursor"
BATCH_EVERY = 2.0                # s
CURSOR_EVERY = 60.0              # s between cursor writes (eMMC)
BUFFER_MAX = 50000               # entries held while the database is away
HOST = socket.gethostname()

DDL = """
CREATE TABLE IF NOT EXISTS eva_log (
    id     bigserial PRIMARY KEY,
    ts     timestamptz NOT NULL,
    host   text,
    unit   text,
    prio   smallint,
    msg    text,
    cursor text UNIQUE
);
CREATE INDEX IF NOT EXISTS eva_log_ts ON eva_log (ts);
CREATE INDEX IF NOT EXISTS eva_log_unit_ts ON eva_log (unit, ts);
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def reader(q):
    """journalctl -> parsed entries on the queue; restarted if it dies."""
    while True:
        cmd = ["journalctl", "-f", "-o", "json", "--no-pager"]
        try:
            cur = open(CURSOR_FILE).read().strip()
        except OSError:
            cur = ""
        cmd += ["--after-cursor", cur] if cur else ["-b", "-n", "all"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
        for line in p.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("MESSAGE")
            if isinstance(msg, list):          # non-UTF-8 message: journald gives bytes
                msg = bytes(msg).decode("utf-8", "replace")
            q.put((int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6,
                   e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or e.get("_COMM"),
                   int(e.get("PRIORITY", 6)), msg, e.get("__CURSOR")))
        p.wait()
        log("journalctl ended, restarting")
        time.sleep(2)


def main():
    import psycopg
    dsn = secrets().get("ASSISTANT_PG_DSN")
    if not dsn:
        log("no ASSISTANT_PG_DSN")
        return 1
    q = queue.Queue()
    threading.Thread(target=reader, args=(q,), daemon=True).start()
    conn, buf, last_cursor, cursor_at = None, [], None, 0.0
    while True:
        time.sleep(BATCH_EVERY)
        while True:
            try:
                buf.append(q.get_nowait())
            except queue.Empty:
                break
        if len(buf) > BUFFER_MAX:
            del buf[:len(buf) - BUFFER_MAX]
        if not buf:
            continue
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
                conn.execute(DDL)
                log("connected")
            with conn.cursor() as c:
                c.executemany("INSERT INTO eva_log (ts, host, unit, prio, msg, cursor)"
                              " VALUES (to_timestamp(%s), %s, %s, %s, %s, %s) ON CONFLICT (cursor) DO NOTHING",
                              [(ts, HOST, unit, prio, msg, cur) for ts, unit, prio, msg, cur in buf])
            last_cursor = buf[-1][4] or last_cursor
            buf = []
        except Exception as e:
            log("db:", str(e)[:200])
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            continue
        if last_cursor and time.time() - cursor_at > CURSOR_EVERY:
            try:
                with open(CURSOR_FILE + ".tmp", "w") as f:
                    f.write(last_cursor)
                os.replace(CURSOR_FILE + ".tmp", CURSOR_FILE)
                cursor_at = time.time()
            except OSError as e:
                log("cursor:", e)


if __name__ == "__main__":
    sys.exit(main())
