"""Skill "энергия": the Home Power Meter on shn-linux (D:/PRG/Pyton/Home Power
Meter, :8484) - reading only. Its own API computes the energy (two methods per
device type, Tuya-cloud verification, charger sessions), so Eva's numbers are
the ones its dashboard shows; raw SQL over samples would not be.

Switching sockets is a different tool (docs/ARCHITECTURE.md: needs adult and a
confirmation) - not here yet."""
import datetime
import json
import os
import urllib.parse

from core import profile
import urllib.request

from core.skills import Skill, Tool

API = profile.service("power")
MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _get(path, timeout=40):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.loads(r.read())


def _devices():
    s = _get("/api/summary")
    out = []
    for group, kind in (("devices", "потребитель"), ("generator_devices", "генерация")):
        for d in s.get(group) or []:
            out.append({"id": d["device_id"], "name": d["name"], "room": d.get("room") or "", "kind": kind})
    return out, s


def _find(name, devices):
    n = name.lower().strip()
    hit = [d for d in devices if n in d["name"].lower()] or \
          [d for d in devices if any(w in d["name"].lower() for w in n.split() if len(w) > 2)]
    return hit[0] if hit else None


def devices(args, person):
    ds, s = _devices()
    head = (f"В этом месяце: потреблено {s.get('consumed_energy_kwh')} кВт·ч, сгенерировано "
            f"{s.get('generated_energy_kwh')} кВт·ч, оценка стоимости {s.get('estimated_cost')}.")
    return head + "\nУстройства: " + "; ".join(f"{d['name']} ({d['kind']}, {d['room']})" for d in ds)


def usage(args, person):
    """Energy of the named devices over a date range, in the app's own buckets
    (hours for a day, days for up to ~2 months, months beyond)."""
    ds, _ = _devices()
    start, end = args["start"], args["end"]
    out = {}
    for name in args["devices"]:
        d = _find(name, ds)
        if not d:
            out[name] = "нет такого устройства"
            continue
        q = urllib.parse.urlencode({"period": "custom", "start": start, "end": end})
        st = _get(f"/api/devices/{d['id']}/stats?{q}")
        bucket = st["period"].get("bucket")
        rows = []
        for x in st["series"]:
            t = datetime.datetime.fromisoformat(x["timestamp"])
            label = (f"{t:%Y-%m}" if bucket == "month" else f"{t:%Y-%m-%d}" if bucket == "day" else f"{t:%H}:00")
            rows.append([label, round(x["energy_kwh"], 2)])
        out[d["name"]] = {"bucket": bucket, "total_kwh": st["summary"].get("energy_kwh"),
                          "peak_kw": st["summary"].get("peak_power_kw"), "series": rows}
    return json.dumps(out, ensure_ascii=False)


SKILL = Skill(
    name="power",
    summary="энергопотребление дома: устройства, кВт·ч по месяцам/дням/часам, генерация",
    prompt="""## Энергия (домашний счётчик)

power_devices - список устройств (розетки, автоматы, зарядки машин TESLA и HONDA,
солнечная генерация) и сводка за месяц. power_usage - кВт·ч выбранных устройств
за период: по часам за день, по дням за месяц-два, по месяцам за больший срок.
Цифры считает само приложение счётчика - не пересчитывай их. Чтобы показать
график, используй show_html.""",
    tools=[Tool(
        name="power_devices",
        description="Список устройств домашнего счётчика электроэнергии и сводка за текущий месяц "
                    "(потреблено, сгенерировано, стоимость).",
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=devices,
        needs="adult",
    ), Tool(
        name="power_usage",
        description="Потребление электроэнергии (кВт·ч) устройств за период дат, ряд по часам/дням/месяцам "
                    "(шаг выбирается по длине периода). devices - названия как в power_devices "
                    "(например «TESLA», «HONDA»). start/end - YYYY-MM-DD включительно.",
        schema={"type": "object", "additionalProperties": False, "required": ["devices", "start", "end"],
                "properties": {"devices": {"type": "array", "items": {"type": "string"}},
                               "start": {"type": "string"}, "end": {"type": "string"}}},
        handler=usage,
        needs="adult",
    )],
)
