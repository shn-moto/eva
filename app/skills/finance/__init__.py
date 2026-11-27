"""Skill "финансы": the family's bank statements in home-finances on shn-linux
(a home-finances service, :8686) - reading only, through its API, so the
categories and sums are the ones its portal shows. Money is private: adults,
at home by voice ("private" is locked in Telegram, except for the accounts
that are admins in photolib - core/identity.py)."""
import datetime
import json
import os
import urllib.parse
import urllib.request

from core import profile
from core.skills import Skill, Tool

API = profile.service("finance")


def _get(path, **q):
    q = {k: v for k, v in q.items() if v not in (None, "")}
    with urllib.request.urlopen(API + path + ("?" + urllib.parse.urlencode(q) if q else ""), timeout=30) as r:
        return json.loads(r.read())


def _months(a, b):
    y, m = map(int, a.split("-"))
    y2, m2 = map(int, b.split("-"))
    while (y, m) <= (y2, m2):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def breakdown(args, person):
    d = _get("/api/breakdown", period=args["period"], kind=args.get("kind") or "expense", user=args.get("user"))
    t = d.get("totals") or {}
    cats = "; ".join(f"{c['category']}: {c['total']} ({c['count']})" for c in d.get("categories", []))
    return (f"{args['period']}: доход {t.get('income')}, расход {t.get('expense')}, итог {t.get('net')}{' ' + profile.CURRENCY if profile.CURRENCY else ''}. "
            f"По категориям (сумма, число операций): {cats}")


def series(args, person):
    """One category month by month (or all of it): the loop is here, not in a
    dozen tool calls of hers."""
    kind = args.get("kind") or "expense"
    want = [c.lower() for c in args.get("categories") or []]
    rows = []
    for month in _months(args["from_month"], args["to_month"]):
        d = _get("/api/breakdown", period=month, kind=kind, user=args.get("user"))
        row = {"month": month, "total": (d.get("totals") or {}).get(kind if kind != "all" else "net")}
        for c in d.get("categories", []):
            if not want or c["category"].lower() in want or any(w in c["category"].lower() for w in want):
                row[c["category"]] = c["total"]
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False)


def transactions(args, person):
    d = _get("/api/transactions", period=args["period"], category=args.get("category"), user=args.get("user"))
    items = d.get("transactions", d) if isinstance(d, dict) else d
    items = items[: int(args.get("limit") or 30)]
    return json.dumps(items, ensure_ascii=False)[:6000]


PROMPT = """## Finances (the family's bank statements)

Sums are in the account currency""" + (f" ({profile.CURRENCY})" if profile.CURRENCY else "") + """, computed by the
home-finances portal. user - whose account: """ + ", ".join(f"{k} ({v})" for k, v in profile.FINANCE_USERS.items()) + """; without it - all."""

_USER = {"type": "string", "enum": list(profile.FINANCE_USERS) or ["me"]}

SKILL = Skill(
    name="finance",
    summary="финансы семьи: расходы и доходы по категориям, по месяцам, операции",
    prompt=PROMPT,
    tools=[Tool(
        name="finance_breakdown",
        description="Income, spending and the total for a period, by category. period: YYYY-MM or YYYY.",
        schema={"type": "object", "additionalProperties": False, "required": ["period"],
                "properties": {"period": {"type": "string"},
                               "kind": {"type": "string", "enum": ["expense", "income", "all"]},
                               "user": _USER}},
        handler=breakdown,
        needs="private",
    ), Tool(
        name="finance_series",
        description=("Monthly sums for a period (for charts and comparisons): the total and chosen categories "
                     '(e.g. ["Продукты"]); without categories - all. from_month/to_month - YYYY-MM.'),
        schema={"type": "object", "additionalProperties": False, "required": ["from_month", "to_month"],
                "properties": {"from_month": {"type": "string"}, "to_month": {"type": "string"},
                               "categories": {"type": "array", "items": {"type": "string"}},
                               "kind": {"type": "string", "enum": ["expense", "income", "all"]},
                               "user": _USER}},
        handler=series,
        needs="private",
    ), Tool(
        name="finance_transactions",
        description=("Transactions for a month (YYYY-MM), optionally by category: date, sum, description, "
                     "category."),
        schema={"type": "object", "additionalProperties": False, "required": ["period"],
                "properties": {"period": {"type": "string"}, "category": {"type": "string"},
                               "user": _USER, "limit": {"type": "integer"}}},
        handler=transactions,
        needs="private",
    )],
)
