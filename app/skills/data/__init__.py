"""Skill "данные": read-only SQL over the home's databases.

Eva gets the database itself, not a call per question: she reads the schema,
writes the query, looks at the result and goes on - the way a person with
psql would. Which databases, what is in them and who may ask is in the
profile ("databases"); the connection strings are secrets.

    "databases": [{"name": "home", "dsn": "HOME_DB_DSN", "needs": "known",
                   "about": "what is there, in a sentence or two"}]

Read-only three times over: the database role itself (SELECT grants only,
default_transaction_read_only, statement_timeout; secrets such as device keys
and password hashes not granted), a READ ONLY transaction here, and a cap on
the rows and characters returned."""
import datetime
import decimal
import json
import threading
import time

from core import profile
from core.skills import Skill, Tool
from core.util import log, secrets

DBS = {d["name"]: d for d in profile.DATABASES if d.get("name") and d.get("dsn")}
MAX_ROWS = profile.tune("data.max_rows", 200)
MAX_CHARS = profile.tune("data.max_chars", 12000)
TIMEOUT_MS = profile.tune("data.timeout_ms", 20000)
_schema = {}
_lock = threading.Lock()

SCHEMA_SQL = """
select c.table_schema, c.table_name, c.column_name, c.data_type,
       coalesce(col_description(format('%I.%I', c.table_schema, c.table_name)::regclass, c.ordinal_position), '')
from information_schema.columns c
where c.table_schema not in ('pg_catalog', 'information_schema')
  and c.table_schema not like '\\_timescaledb%' and c.table_schema not in ('timescaledb_information',
      'timescaledb_experimental', '_timescaledb_cache', '_timescaledb_catalog', '_timescaledb_config',
      '_timescaledb_functions', '_timescaledb_internal')
  and has_column_privilege(format('%I.%I', c.table_schema, c.table_name), c.column_name, 'SELECT')
order by c.table_schema, c.table_name, c.ordinal_position
"""

ROWS_SQL = """
select n.nspname, c.relname, greatest(c.reltuples, 0)::bigint, coalesce(obj_description(c.oid), '')
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p', 'v', 'm') and n.nspname not in ('pg_catalog', 'information_schema')
"""


def _connect(db):
    import psycopg
    dsn = secrets().get(DBS[db]["dsn"])
    if not dsn:
        raise LookupError(f"нет строки подключения {DBS[db]['dsn']} в секретах")
    conn = psycopg.connect(dsn, connect_timeout=8, options=f"-c statement_timeout={TIMEOUT_MS}")
    conn.read_only = True
    return conn


def _check(db, person):
    if db not in DBS:
        return f"нет базы «{db}»; есть: {', '.join(DBS) or 'никаких'}"
    if not person.may(DBS[db].get("needs", "adult")):
        return "эта база только для взрослых" if DBS[db].get("needs", "adult") == "adult" else "эта база тебе недоступна"
    return None


def _hypertables(conn):
    """TimescaleDB: a hypertable's own reltuples is 0, its rows live in chunks."""
    try:
        rows = conn.execute("select hypertable_schema, hypertable_name from timescaledb_information.hypertables").fetchall()
    except Exception:
        conn.rollback()
        return {}
    out = {}
    for s, t in rows:
        try:
            name = '"' + s.replace('"', '""') + '"."' + t.replace('"', '""') + '"'
            out[(s, t)] = conn.execute("select approximate_row_count(%s::regclass)", (name,)).fetchone()[0]
        except Exception:
            conn.rollback()
    return out


def schema(args, person):
    db = args["db"]
    err = _check(db, person)
    if err:
        return err
    with _lock:
        hit = _schema.get(db)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
    with _connect(db) as conn:
        cols = conn.execute(SCHEMA_SQL).fetchall()
        sizes = {(s, t): (n, d) for s, t, n, d in conn.execute(ROWS_SQL).fetchall()}
        hyper = _hypertables(conn)
    tables = {}
    for s, t, col, typ, note in cols:
        tables.setdefault((s, t), []).append(f"{col} {typ}" + (f" -- {note}" if note else ""))
    lines = []
    for (s, t), cs in tables.items():
        n, note = sizes.get((s, t), (0, ""))
        n = hyper.get((s, t), n)
        name = t if s == "public" else f"{s}.{t}"
        lines.append(f"{name} (~{n} строк" + (", hypertable TimescaleDB" if (s, t) in hyper else "") + ")"
                     + (f" -- {note}" if note else "") + "\n  " + ", ".join(cs))
    text = "\n".join(lines) or "таблиц не видно"
    with _lock:
        _schema[db] = (time.time(), text)
    return text


def _cell(v):
    if isinstance(v, datetime.datetime):
        if v.tzinfo:
            v = v.astimezone()
        return v.strftime("%Y-%m-%d %H:%M:%S").removesuffix(":00")
    if isinstance(v, (datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, decimal.Decimal):
        return float(round(v, 4))
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, (bytes, memoryview)):
        return f"<{len(bytes(v))} байт>"
    return v


def query(args, person):
    db, sql = args["db"], args["sql"].strip().rstrip(";")
    err = _check(db, person)
    if err:
        return err
    t = time.time()
    try:
        with _connect(db) as conn:
            cur = conn.execute(sql)
            if cur.description is None:
                return "запрос ничего не вернул"
            head = [c.name for c in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
    except Exception as e:
        msg = str(e).strip().split("\n")[0][:400]
        log(f"data {db}: error {msg}")
        return f"ошибка: {msg}"
    more = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    out = json.dumps({"columns": head, "rows": [[_cell(v) for v in r] for r in rows]},
                     ensure_ascii=False, separators=(",", ":"), default=str)
    if more:
        out += f"\n[показаны первые {MAX_ROWS} строк - агрегируй (count, avg, date_trunc / time_bucket) или сузь where]"
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS] + f"\n[обрезано на {MAX_CHARS} символах - выбери меньше колонок или агрегируй]"
    log(f"data {db}: {len(rows)} rows in {int((time.time() - t) * 1000)} ms")
    return out


def _prompt():
    if not DBS:
        return ""
    lines = [f"- {n}: {d.get('about', '').strip()}" for n, d in DBS.items()]
    return ("## Данные: базы дома (только чтение)\n\n"
            "Базы:\n" + "\n".join(lines) + "\n\n"
            "Ответ на вопрос о том, что записано (датчики, история, счётчики, что угодно), ищи в базе сама,\n"
            "как человек с psql: db_schema - таблицы и колонки, потом db_query - SQL (PostgreSQL), сколько\n"
            "нужно шагов. Не знаешь, как хранится, - посмотри несколько строк (limit 5) и разберись: коды,\n"
            "json-поля, справочники соседних таблиц. Агрегируй в SQL (avg/min/max, date_trunc, time_bucket),\n"
            "не тащи тысячи точек. Время в базе - с часовым поясом, отвечай по местному. Смотри на свежесть:\n"
            "последняя запись давно - так и скажи. Менять ничего нельзя: база только на чтение.")


SKILL = Skill(
    name="data",
    summary="базы данных дома (только чтение): " + ", ".join(f"{n} - {d.get('about', '')[:60]}" for n, d in DBS.items()),
    prompt=_prompt(),
    tools=[Tool(
        name="db_schema",
        description="Таблицы и колонки базы (с примерным числом строк и комментариями). Начни с него, "
                    "если ещё не знаешь, как устроена база.",
        schema={"type": "object", "additionalProperties": False, "required": ["db"],
                "properties": {"db": {"type": "string", "enum": list(DBS) or ["-"]}}},
        handler=schema,
        needs="known",
    ), Tool(
        name="db_query",
        description="SQL-запрос к базе (PostgreSQL, только чтение - select/with). Возвращает колонки и "
                    f"до {MAX_ROWS} строк; время - местное. Большие выборки агрегируй в самом запросе.",
        schema={"type": "object", "additionalProperties": False, "required": ["db", "sql"],
                "properties": {"db": {"type": "string", "enum": list(DBS) or ["-"]},
                               "sql": {"type": "string"}}},
        handler=query,
        needs="known",
    )] if DBS else [],
)
