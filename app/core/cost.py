"""What Eva costs: every Claude call (every round of a tool loop too) priced from
its usage, totals per day on disk, and a daily limit - after it she says so
once and stays off Claude until midnight (Иван, 2026-11-20: $5.6 in a day,
the cache was missing; see core/llm.py _params).

Prices per million tokens (claude-api skill, 2026-11): cache writes 1.25x the
input price for the 5-minute TTL, 2x for the 1-hour one; reads as listed."""
import json
import os
import threading
import time

from core.util import log
from core import profile

SPEND_FILE = "/var/lib/eva/spend.json"
DAILY_LIMIT = profile.tune("cost.daily_limit", 3.0)      # USD a day
PRICES = {   # model: (input, output, cache read)
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
_lock = threading.Lock()


class OverBudget(Exception):
    """Today's limit is spent."""


def _today():
    return time.strftime("%Y-%m-%d")


def _load():
    try:
        return json.load(open(SPEND_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d):
    tmp = SPEND_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, SPEND_FILE)
    except OSError as e:
        log("spend save error:", e)


def price(model, usage):
    """USD of one call from its usage."""
    pin, pout, pread = PRICES.get(model, PRICES["claude-opus-5-5"])
    cc = getattr(usage, "cache_creation", None)
    w1h = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0 if cc else 0
    w5m = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0 if cc else 0
    written = usage.cache_creation_input_tokens or 0
    if not (w1h or w5m):
        w5m = written                          # no breakdown: the cheaper TTL
    return ((usage.input_tokens or 0) * pin + (usage.output_tokens or 0) * pout
            + (usage.cache_read_input_tokens or 0) * pread + w5m * pin * 1.25 + w1h * pin * 2.0) / 1e6


on_change = None      # set by the brain: the TV's status bar gets the new numbers


def add(model, usage, channel="voice"):
    """Count one call. -> its price."""
    usd = price(model, usage)
    with _lock:
        d = _load()
        day = d.setdefault(_today(), {"usd": 0.0, "calls": 0, "by_model": {}, "by_channel": {}})
        day["usd"] = round(day["usd"] + usd, 5)
        day["calls"] += 1
        day["by_model"][model] = round(day["by_model"].get(model, 0.0) + usd, 5)
        day["by_channel"][channel] = round(day["by_channel"].get(channel, 0.0) + usd, 5)
        # tokens by model: what the status bar shows (cache read against written)
        t = day.setdefault("tokens", {}).setdefault(model, {"calls": 0, "in": 0, "out": 0, "read": 0, "written": 0})
        t["calls"] += 1
        t["in"] += usage.input_tokens or 0
        t["out"] += usage.output_tokens or 0
        t["read"] += usage.cache_read_input_tokens or 0
        t["written"] += usage.cache_creation_input_tokens or 0
        for k in sorted(d)[:-31]:              # a month of days is enough
            del d[k]
        _save(d)
    if on_change:
        try:
            on_change()
        except Exception as e:
            log("spend on_change:", e)
    return usd


def stats():
    """The status bar's event: today, the month, tokens and cache by model."""
    d = _load()
    day = d.get(_today(), {})
    month = sum(v.get("usd", 0.0) for k, v in d.items() if k.startswith(_today()[:7]))
    models = {}
    for m, t in day.get("tokens", {}).items():
        models[m.replace("claude-", "").replace("-5-5", "")] = dict(t, usd=round(day["by_model"].get(m, 0.0), 4))
    return {"type": "stats", "today": round(day.get("usd", 0.0), 4), "calls": day.get("calls", 0),
            "month": round(month, 4), "limit": DAILY_LIMIT, "models": models}


def today():
    return _load().get(_today(), {"usd": 0.0, "calls": 0, "by_model": {}, "by_channel": {}})


def check():
    """Before a call: raise OverBudget once today's limit is spent."""
    if DAILY_LIMIT > 0 and today()["usd"] >= DAILY_LIMIT:
        raise OverBudget(f"{today()['usd']:.2f} of {DAILY_LIMIT:.2f} USD")


def summary(days=7):
    d = _load()
    lines = []
    for k in sorted(d)[-days:]:
        v = d[k]
        lines.append(f"{k}: ${v['usd']:.2f}, вызовов {v['calls']}, "
                     + ", ".join(f"{m.replace('claude-', '')} ${x:.2f}" for m, x in v["by_model"].items()))
    return "\n".join(lines) or "Трат пока нет."
