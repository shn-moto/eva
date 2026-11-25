"""Small shared helpers: logging, secrets, the date in Russian."""
import os
import queue
import socket
import threading
import time
import uuid

SECRETS = os.environ.get("EVA_SECRETS", "/etc/eva/secrets.env")
RU_DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
             "октября", "ноября", "декабря")


def log(*a):
    line = " ".join(str(x) for x in a)
    print(time.strftime("%H:%M:%S"), line, flush=True)
    if LOG_UNIT:
        try:
            _logq.put_nowait((time.time(), line))
        except queue.Full:
            pass


# On shn-linux (the brain's container) there is no Station journal for logship.py
# to follow: the lines go to the same table, assistant_memory.eva_log, from here,
# under the unit's name - the admin page reads them as before.
LOG_UNIT = os.environ.get("EVA_LOG_UNIT", "")
_logq = queue.Queue(maxsize=50000)


def _log_shipper():
    import psycopg
    host, conn = socket.gethostname(), None
    while True:
        batch = [_logq.get()]
        time.sleep(1.0)
        while not _logq.empty() and len(batch) < 2000:
            batch.append(_logq.get_nowait())
        for _ in range(3):
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(secrets()["ASSISTANT_PG_DSN"], autocommit=True, connect_timeout=5)
                with conn.cursor() as c:
                    c.executemany("INSERT INTO eva_log (ts, host, unit, prio, msg, cursor) VALUES "
                                  "(to_timestamp(%s), %s, %s, 6, %s, %s) ON CONFLICT DO NOTHING",
                                  [(t, host, LOG_UNIT, m, uuid.uuid4().hex) for t, m in batch])
                break
            except Exception as e:
                print("log shipping:", e, flush=True)
                conn = None
                time.sleep(5)


if LOG_UNIT:
    threading.Thread(target=_log_shipper, daemon=True).start()


def secrets():
    with open(SECRETS, encoding="utf-8") as f:
        return dict(l.strip().split("=", 1) for l in f if "=" in l and not l.startswith("#"))


def ru_now():
    """'четверг, 19 ноября 2026, 09:15' - the model reads a Russian date reliably
    (with '%A %d.%m.%Y' it once counted a birthday as tomorrow)."""
    t = time.localtime()
    return f"{RU_DAYS[t.tm_wday]}, {t.tm_mday} {RU_MONTHS[t.tm_mon - 1]} {t.tm_year}, {t.tm_hour:02d}:{t.tm_min:02d}"
