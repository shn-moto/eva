"""Skill "дом": the home app (Home Power Meter, service "power") - reading only.

- Energy: power_devices / power_usage. The app's own API computes it (two methods
  per device type, cloud verification, charger sessions), so Eva's numbers are
  the ones its dashboard shows; raw SQL over samples would not be.
- Everything else (sensors and their history, meter readings, automations):
  home_api_index lists the app's GET /api endpoints from its OpenAPI spec and
  home_api_get calls any of them. Eva explores instead of a fixed call per
  question, and a new section of the app needs no change here.

Switching sockets is a different tool (docs/ARCHITECTURE.md: needs adult and a
confirmation) - not here yet."""
import datetime
import json
import os
import urllib.parse

from core import profile
import urllib.error
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


_index = {"at": 0, "text": ""}
INDEX_TTL = 600
# Whole responses can be long (series of a day of samples): the model gets
# this much and is told to narrow the request (period, start/end, view).
API_MAX = profile.tune("power.api_max_chars", 14000)


def _schema_type(p):
    s = p.get("schema") or {}
    t = s.get("type") or "/".join(x.get("type", "") for x in s.get("anyOf", []) if x.get("type") != "null")
    d = f"={s['default']}" if "default" in s else ""
    return f"{p['name']}:{t or 'any'}{d}"


def api_index(args, person):
    """The app's read-only API, from its own OpenAPI description - so a new
    section of the app (sensors, meters, automations) is visible to Eva without
    a change here."""
    import time
    if time.time() - _index["at"] > INDEX_TTL or not _index["text"]:
        spec = _get("/openapi.json")
        lines = []
        for path, methods in spec.get("paths", {}).items():
            g = methods.get("get")
            if not g or not path.startswith("/api/"):
                continue
            params = ", ".join(_schema_type(p) for p in g.get("parameters", []))
            doc = (g.get("description") or g.get("summary") or "").strip().split("\n")[0][:120]
            lines.append(f"GET {path}" + (f" ({params})" if params else "") + (f" - {doc}" if doc else ""))
        _index.update(at=time.time(), text="\n".join(lines))
    return _index["text"]


SERIES_POINTS = profile.tune("power.series_points", 96)


def _is_series(x):
    """A time series as the app sends it: [[epoch_ms, value], ...]."""
    return (isinstance(x, list) and len(x) > 8 and all(isinstance(p, list) and len(p) == 2 for p in x[:3])
            and isinstance(x[0][0], (int, float)) and x[0][0] > 1e12)


def _when(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _thin(series):
    """Any long series -> at most SERIES_POINTS bucket means with local times,
    plus min / max / last over all of it (the model reads trends, not 2880 samples)."""
    pts = [p for p in series if p[1] is not None]
    nums = [p for p in pts if isinstance(p[1], (int, float)) and not isinstance(p[1], bool)]
    if not nums:                    # on/off and text states: keep the changes only
        changes = [p for i, p in enumerate(pts) if i == 0 or p[1] != pts[i - 1][1]]
        return {"changes": [[_when(t), v] for t, v in changes[-SERIES_POINTS:]], "points": len(pts)}
    step = max(1, -(-len(nums) // SERIES_POINTS))
    thin = []
    for i in range(0, len(nums), step):
        chunk = nums[i:i + step]
        thin.append([_when(chunk[0][0]), round(sum(v for _, v in chunk) / len(chunk), 2)])
    lo = min(nums, key=lambda p: p[1])
    hi = max(nums, key=lambda p: p[1])
    return {"min": [_when(lo[0]), round(lo[1], 2)], "max": [_when(hi[0]), round(hi[1], 2)],
            "last": [_when(nums[-1][0]), round(nums[-1][1], 2)],
            "points": len(nums), "every_min": round(step * (nums[-1][0] - nums[0][0]) / max(len(nums) - 1, 1) / 60000, 1),
            "series": thin}


def _compact(x):
    if _is_series(x):
        return _thin(x)
    if isinstance(x, dict):
        return {k: _compact(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_compact(v) for v in x]
    return x


def api_get(args, person):
    path = args["path"].strip()
    if not path.startswith("/api/") or ".." in path or "?" in path:
        return "путь должен начинаться с /api/, параметры - в params"
    params = {k: v for k, v in (args.get("params") or {}).items() if v not in (None, "")}
    url = path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        data = _get(url)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read()[:500].decode('utf-8', 'replace')}"
    text = json.dumps(_compact(data), ensure_ascii=False, separators=(",", ":"))
    if len(text) > API_MAX:
        text = text[:API_MAX] + f" …[обрезано: всего {len(text)} символов - сузь запрос: период, start/end, view]"
    return text


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
    summary="дом: электричество (устройства, кВт·ч, генерация) и датчики (температура, влажность, "
            "движение, двери) с историей - всё, что пишет домашнее приложение",
    prompt="""## Home: electricity and sensors (the home app)

The home app collects everything: meters and sockets, car chargers, solar, sensors
(temperature, humidity, motion, doors, gateway) with history, meter readings,
automations. Rooms are in the devices' room field.
- power_devices / power_usage are ready-made for electricity (the app computes the
  numbers - do not recompute them).
- For anything else - home_api_index, then home_api_get. Not sure where the answer
  is - open the index and find it: «температура в детской» - the sensor summary, the
  device with room «Детская»; history - its sensor-history for the period.
- Mind freshness: last_seen / last_seen_status. Give an old value with its time
  («последнее, что пришло, 1 августа»), not as current.
To show a chart, use show_html.""",
    tools=[Tool(
        name="home_api_index",
        description=("All sections (GET /api/...) of the home app with parameters: electricity, sensors and "
                     "their history, meter readings, automations. Start here if you do not know where the data "
                     "is."),
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=api_index,
        needs="known",
    ), Tool(
        name="home_api_get",
        description=("GET request to the home app (read-only). path - from home_api_index with {path "
                     "parameters} filled in, e.g. /api/sensors/summary or "
                     "/api/devices/<device_id>/sensor-history; params - query parameters (period, start, end, "
                     "view...). A long answer is cut - then narrow the period."),
        schema={"type": "object", "additionalProperties": False, "required": ["path"],
                "properties": {"path": {"type": "string"},
                               "params": {"type": "object", "additionalProperties": {"type": "string"}}}},
        handler=api_get,
        needs="known",
    ), Tool(
        name="power_devices",
        description="Devices of the home electricity meter and this month's summary (used, generated, cost).",
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=devices,
        needs="adult",
    ), Tool(
        name="power_usage",
        description=("Electricity use (kWh) of devices over a date range, hourly/daily/monthly (step chosen by "
                     "the length). devices - names as in power_devices (e.g. «TESLA», «HONDA»). start/end - "
                     "YYYY-MM-DD inclusive."),
        schema={"type": "object", "additionalProperties": False, "required": ["devices", "start", "end"],
                "properties": {"devices": {"type": "array", "items": {"type": "string"}},
                               "start": {"type": "string"}, "end": {"type": "string"}}},
        handler=usage,
        needs="adult",
    )],
)
