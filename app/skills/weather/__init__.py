"""Skill "погода": the forecast from Open-Meteo (free, no key, a forecast model,
not pages). A web search for the weather brought cached pages a month old (a
"forecast" from September for a November week, 2026-11-25) and many steps;
this is one call, fresh, for home (profile household.weather: lat, lon, name)
or any place by its name (Open-Meteo's geocoder)."""
import json
import time
import urllib.parse
import urllib.request

from core import profile
from core.skills import Skill, Tool

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
HOME = profile.WEATHER                    # {"lat", "lon", "name"} - private, in the household profile
TZ = profile.LOCATION.get("timezone", "auto")
_cache = {}                               # url -> (time, data): 15 minutes

# WMO weather codes (Open-Meteo)
WMO = {0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман", 48: "изморозь, туман",
       51: "слабая морось", 53: "морось", 55: "сильная морось", 56: "ледяная морось", 57: "сильная ледяная морось",
       61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "сильный ледяной дождь",
       71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа", 80: "небольшой ливень",
       81: "ливень", 82: "сильный ливень", 85: "небольшой снегопад", 86: "сильный снегопад", 95: "гроза",
       96: "гроза с градом", 99: "сильная гроза с градом"}
DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

PROMPT = """## Weather

Any weather question («тепло ли будет», «брать ли зонт») - only the weather tool,
never a web search: search pages are often stale. Say it in your own words, briefly,
not every number: day and night temperature, precipitation, wind if strong."""


def _get(url):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    _cache[url] = (time.time(), data)
    return data


def _place(name):
    if not name:
        if not HOME:
            return None, "Дом не задан в профиле (household.weather) - назови город."
        return (HOME["lat"], HOME["lon"], HOME.get("name", "дома")), None
    q = urllib.parse.urlencode({"name": name, "count": 1, "language": "ru"})
    res = (_get(f"{GEOCODE}?{q}").get("results") or [])
    if not res:
        return None, f"Не нашла место «{name}»."
    r = res[0]
    label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
    return (r["latitude"], r["longitude"], label), None


def weather(args, person):
    where, err = _place((args.get("place") or "").strip())
    if err:
        return err
    lat, lon, label = where
    days = max(1, min(16, int(args.get("days") or 7)))
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": TZ, "forecast_days": days, "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m,wind_gusts_10m,relative_humidity_2m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
                 "snowfall_sum,wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset",
        "hourly": "temperature_2m,precipitation_probability,weather_code"})
    d = _get(f"{FORECAST}?{q}")
    c = d.get("current", {})
    out = [f"Прогноз Open-Meteo для {label} (обновлён {c.get('time', '').replace('T', ' ')}):",
           f"Сейчас {c.get('temperature_2m')}° (ощущается {c.get('apparent_temperature')}°), "
           f"{WMO.get(c.get('weather_code'), c.get('weather_code'))}, ветер {c.get('wind_speed_10m')} м/с"
           f" (порывы {c.get('wind_gusts_10m')}), влажность {c.get('relative_humidity_2m')}%."]
    if args.get("hourly"):
        h = d.get("hourly", {})
        now = c.get("time", "")[:13]
        idx = next((i for i, t in enumerate(h.get("time", [])) if t[:13] >= now), 0)
        rows = [f"{h['time'][i][11:16]} {h['temperature_2m'][i]}° {WMO.get(h['weather_code'][i], '')}"
                + (f", осадки {h['precipitation_probability'][i]}%" if h["precipitation_probability"][i] else "")
                for i in range(idx, min(idx + 24, len(h.get("time", []))), 3)]
        out.append("Ближайшие сутки: " + "; ".join(rows) + ".")
    dl = d.get("daily", {})
    for i, day in enumerate(dl.get("time", [])):
        wd = DAYS[time.strptime(day, "%Y-%m-%d").tm_wday]
        snow = dl["snowfall_sum"][i]
        out.append(f"{wd} {day[8:10]}.{day[5:7]}: {dl['temperature_2m_min'][i]:+.0f}…{dl['temperature_2m_max'][i]:+.0f}°, "
                   f"{WMO.get(dl['weather_code'][i], dl['weather_code'][i])}, осадки {dl['precipitation_sum'][i]} мм "
                   f"({dl['precipitation_probability_max'][i]}%)" + (f", снег {snow} см" if snow else "")
                   + f", ветер до {dl['wind_speed_10m_max'][i]} м/с (порывы {dl['wind_gusts_10m_max'][i]})")
    return "\n".join(out)


SKILL = Skill(
    name="weather",
    summary="погода: свежий прогноз на 1-16 дней (Open-Meteo), дома или где угодно",
    prompt=PROMPT,
    tools=[Tool(
        name="weather",
        description=("Fresh weather forecast (Open-Meteo): now, daily for 1-16 days, optionally 3-hourly for "
                     "the next day. No place - home; place - any city by name. For any weather question - "
                     "instead of a search."),
        schema={"type": "object", "additionalProperties": False,
                "properties": {"place": {"type": "string", "description": "a city, if not home"},
                               "days": {"type": "integer", "description": "forecast days, 1-16 (usually 7)"},
                               "hourly": {"type": "boolean", "description": "add the next 24 hours in 3-hour steps"}}},
        handler=weather,
        needs=None,
    )],
)
