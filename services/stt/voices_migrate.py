#!/usr/bin/env python3
"""Once (2026-11-24): the Station's voice files -> SQL for the hub's voice base.

    python3 voices_migrate.py <dir with speakers.json, speakers_auto.json,
        speaker_clusters.json, eva-self.json> > voices.sql
    docker exec -i smart_photo_db psql -U dev -d assistant_memory < voices.sql

Run as the tables' owner (the schema + grants are in it); refuses to run twice
(the tables must be empty)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from voices import SCHEMA  # noqa: E402

d = sys.argv[1]


def load(n):
    try:
        return json.load(open(os.path.join(d, n), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def arr(e):
    return "'{" + ",".join(f"{float(x):.6f}" for x in e) + "}'"


out = [SCHEMA, "BEGIN;",
       "DO $$ BEGIN IF EXISTS (SELECT 1 FROM voice_prints) OR EXISTS (SELECT 1 FROM voice_guests) THEN "
       "RAISE EXCEPTION 'voice base not empty: migrated already'; END IF; END $$;"]
for src, data in (("enroll", load("speakers.json")), ("auto", load("speakers_auto.json"))):
    for spk, embs in data.items():
        for e in embs:
            out.append(f"INSERT INTO voice_prints (speaker, emb, source) VALUES ({q(spk)}, {arr(e)}, {q(src)});")
for e in load("eva-self.json") or []:
    out.append(f"INSERT INTO voice_prints (speaker, emb, source) VALUES ('Ева', {arr(e)}, 'self');")
for g, c in load("speaker_clusters.json").items():
    out.append(f"INSERT INTO voice_guests (name, embs, count, first, last) VALUES ({q(g)}, {q(json.dumps(c['embs']))}, "
               f"{int(c.get('count', 1))}, to_timestamp({c.get('first', 0)}), to_timestamp({c.get('last', 0)}));")
out.append("COMMIT;")
print("\n".join(out))
