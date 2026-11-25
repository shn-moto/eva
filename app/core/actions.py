"""Everything she did, kept whole (docs/ACTIONS-PLAN.md).

The conversation history keeps short copies (for price and size); she checked
herself against them and "confessed" to guesses she never made, again and
again (a search, a photo, the news, a week's forecast). Here every action -
our tools and Anthropic's server tools (search, a page fetched, the filtering
code) - is kept with its whole input and result, in assistant_memory.eva_actions.
The history's short copy names the action; `action_recall` gives it back whole.

Table (created by its owner once):
    CREATE TABLE eva_actions (id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        channel text, person text, tool text NOT NULL, input jsonb, result text,
        images jsonb, source text, source_date text);
    GRANT SELECT, INSERT ON eva_actions TO assistant; GRANT USAGE ON SEQUENCE eva_actions_id_seq TO assistant;
"""
import json
import threading

from core.util import log, secrets

_lock = threading.Lock()
_conn = [None]


def _db():
    import psycopg
    if _conn[0] is None or _conn[0].closed:
        _conn[0] = psycopg.connect(secrets()["ASSISTANT_PG_DSN"], autocommit=True, connect_timeout=3)
    return _conn[0]


def record(channel, person, tool, inp, result, images=None, source=None, source_date=None):
    """-> the action's number, or None (the base away: the action is still done,
    only its whole copy is not kept)."""
    try:
        with _lock:
            r = _db().execute(
                "INSERT INTO eva_actions (channel, person, tool, input, result, images, source, source_date)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (channel, person, tool, json.dumps(inp, ensure_ascii=False, default=str), result,
                 json.dumps(images) if images else None, source, source_date)).fetchone()
        return r[0]
    except Exception as e:
        log("actions: not kept:", e)
        _conn[0] = None
        return None


def fetch(aid):
    """-> {id, ts, channel, person, tool, input, result, images, source, source_date} or None"""
    with _lock:
        r = _db().execute("SELECT id, ts, channel, person, tool, input, result, images, source, source_date"
                          " FROM eva_actions WHERE id = %s", (int(aid),)).fetchone()
    if not r:
        return None
    keys = ("id", "ts", "channel", "person", "tool", "input", "result", "images", "source", "source_date")
    return dict(zip(keys, r))


def blocks_text(content):
    """A tool result's content (a string or blocks) -> (its text, its pictures)."""
    if isinstance(content, str):
        return content, []
    text, images = [], []
    for b in content or []:
        if b.get("type") == "text":
            text.append(b.get("text", ""))
        elif b.get("type") == "image":
            images.append(b.get("source"))
    return "\n".join(text), images
