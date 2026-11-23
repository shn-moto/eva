# eva.example — the private household files, as examples

The real files live on the device in `/opt/eva/eva/` and are **not in git**: they
hold your family, address, health and money rules. Copy this folder there and
fill it in:

| file | read by | what |
|---|---|---|
| `BOOTSTRAP.md` | app/core/prompt.py | Eva's personality and who is who at home (always in the prompt) |
| `LEGEND.md` | app/core/prompt.py | her story, how she talks about herself |
| `PRIVATE.md` | app/core/prompt.py | only for a recognised adult at home: health, documents, money |
| `names.json` | app/names.py | forms of the family's names (pet names) for the prompt |
| `profile.json` | app/core/profile.py | household (children, name stems, voices, finance users), speech (name fixes, hotwords), services (hub and URLs) |

`deploy/station.sh` never touches `/opt/eva/eva/`.
