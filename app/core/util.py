"""Small shared helpers: logging, secrets, the date in Russian."""
import time

SECRETS = "/etc/eva/secrets.env"
RU_DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
             "октября", "ноября", "декабря")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def secrets():
    with open(SECRETS, encoding="utf-8") as f:
        return dict(l.strip().split("=", 1) for l in f if "=" in l and not l.startswith("#"))


def ru_now():
    """'четверг, 19 ноября 2026, 09:15' - the model reads a Russian date reliably
    (with '%A %d.%m.%Y' it once counted a birthday as tomorrow)."""
    t = time.localtime()
    return f"{RU_DAYS[t.tm_wday]}, {t.tm_mday} {RU_MONTHS[t.tm_mon - 1]} {t.tm_year}, {t.tm_hour:02d}:{t.tm_min:02d}"
