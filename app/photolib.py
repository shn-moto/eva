"""photolib (Smart Photo Indexing on shn-linux): the family's source of truth.

People and the family tree (persons + parent/spouse relations, birth dates,
descriptions), and the users with access (app_user: Telegram id, person, admin).
The same people may talk to Eva: a Telegram account known to photolib is family.

Read only, through photolib's REST API (it trusts requests from the home
network). Cached for a few minutes; if photolib is away, the last good copy is
used.
"""
import datetime
from concurrent.futures import ThreadPoolExecutor
import html
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request

from core import profile

API = profile.service("photolib")
TTL = 300
_cache = {}
_lock = threading.Lock()


def _get(path):
    with _lock:
        hit = _cache.get(path)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    try:
        with urllib.request.urlopen(API + path, timeout=10) as r:
            data = json.loads(r.read())
    except (OSError, ValueError):
        if hit:
            return hit[1]                    # photolib away: last good copy
        raise
    with _lock:
        _cache[path] = (time.time(), data)
    return data


def users():
    u = _get("/admin/users")
    return u.get("users", []) if isinstance(u, dict) else u


def user_by_telegram(telegram_id):
    for u in users():
        if u.get("telegram_id") and str(u["telegram_id"]) == str(telegram_id):
            return u
    return None


def tree():
    t = _get("/persons/family-tree")
    persons = {p["person_id"]: p for p in t.get("persons", [])}
    return persons, t.get("relations", [])


def _text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _date(p, key):
    d = p.get(key)
    if not d:
        return None
    return ("около " if p.get(key + "_approx") else "") + str(d)[:10]


def relatives(pid, persons, relations):
    """{"parents", "children", "spouses", "siblings"} -> [person_id]"""
    parents = [r["person_id_from"] for r in relations if r["relation_type"] == "parent" and r["person_id_to"] == pid]
    children = [r["person_id_to"] for r in relations if r["relation_type"] == "parent" and r["person_id_from"] == pid]
    spouses = [r["person_id_to"] if r["person_id_from"] == pid else r["person_id_from"]
               for r in relations if r["relation_type"] == "spouse" and pid in (r["person_id_from"], r["person_id_to"])]
    siblings = sorted({r["person_id_to"] for r in relations if r["relation_type"] == "parent"
                       and r["person_id_from"] in parents and r["person_id_to"] != pid})
    return {"parents": parents, "children": children, "spouses": spouses, "siblings": siblings}


def describe(pid, persons, relations, with_text=True):
    p = persons.get(pid)
    if not p:
        return ""
    name = lambda i: persons[i]["name"] if i in persons else f"#{i}"
    parts = [f"{p['name']} (#{pid})"]
    b, d = _date(p, "birth_date"), _date(p, "death_date")
    if b:
        parts.append(f"родил(ась)ся {b}")
    if d:
        parts.append(f"умер(ла) {d}")
    rel = relatives(pid, persons, relations)
    for key, label in (("spouses", "супруг(а)"), ("parents", "родители"), ("children", "дети"), ("siblings", "братья/сёстры")):
        if rel[key]:
            parts.append(f"{label}: " + ", ".join(name(i) for i in rel[key]))
    line = "; ".join(parts)
    if with_text and p.get("description"):
        line += f". {_text(p['description'])[:300]}"
    return line


def birthdays(days=14, today=None):
    """Birthdays of living people in the tree from today on, ages computed here
    (the model once got "today" wrong). -> ["сегодня: Иван, 49 лет", ...]"""
    persons, _ = tree()
    today = today or datetime.date.today()
    out = []
    for p in persons.values():
        if not p.get("birth_date") or p.get("death_date") or p.get("birth_date_approx"):
            continue
        try:
            b = datetime.date.fromisoformat(str(p["birth_date"])[:10])
        except ValueError:
            continue
        try:
            nxt = b.replace(year=today.year)
        except ValueError:                       # 29 February
            nxt = datetime.date(today.year, 3, 1)
        if nxt < today:
            nxt = nxt.replace(year=today.year + 1)
        delta = (nxt - today).days
        if delta <= days:
            age = nxt.year - b.year
            when = "сегодня" if delta == 0 else "завтра" if delta == 1 else f"через {delta} дн. ({nxt:%d.%m})"
            out.append((delta, f"{when}: {p['name']}, исполняется {age}"))
    return [t for _, t in sorted(out)]


def family_block():
    """The household and its closest relatives, for Eva's instructions."""
    persons, relations = tree()
    core = []
    for u in users():
        if u.get("person_id") and u["person_id"] not in core:
            core.append(u["person_id"])
    near = list(core)
    for pid in core:
        rel = relatives(pid, persons, relations)
        for key in ("spouses", "children", "parents", "siblings"):
            for i in rel[key]:
                if i not in near:
                    near.append(i)
    lines = [describe(pid, persons, relations, with_text=pid in core) for pid in near]
    who = {}
    for u in users():
        if u.get("person_id"):
            who.setdefault(u["person_id"], []).append(("админ" if u.get("is_admin") else "пользователь")
                                                       + (", есть Telegram" if u.get("telegram_id") else ""))
    access = [f"{persons[pid]['name']}: {', '.join(sorted(set(v)))}" for pid, v in who.items() if pid in persons]
    bd = birthdays()
    bd_text = (f"\n\nДни рождения (посчитано точно, на сегодня {datetime.date.today():%d.%m.%Y}):\n- "
               + "\n- ".join(bd)) if bd else ""
    return ("Семья (родовое дерево из photolib, всего персон в дереве: "
            f"{len(persons)}; здесь - ближний круг):\n- " + "\n- ".join(l for l in lines if l)
            + "\n\nДоступ к тебе (пользователи photolib):\n- " + "\n- ".join(access) + bd_text
            + "\n\nРодовое дерево ты только читаешь: изменить его не можешь. Если чего-то не хватает -"
              " предложи хозяину системы (admin) добавить это в photolib.")


def find(query):
    """person_ids whose name matches the query: whole substring first, then word stems."""
    persons, _ = tree()
    q = query.lower().strip()
    hits = [pid for pid, p in persons.items() if q in p["name"].lower()]
    if not hits:
        words = [w for w in re.findall(r"\w+", q) if len(w) >= 3]
        hits = [pid for pid, p in persons.items() if any(w[:4] in p["name"].lower() for w in words)]
    return hits


def lookup(query):
    """Persons whose name contains the query (case-insensitive), described."""
    persons, relations = tree()
    hits = find(query)
    if not hits:
        return "В родовом дереве такого человека не нашлось."
    return "\n".join(describe(pid, persons, relations) for pid in hits[:8])


# ------------------------------------------------------------ photos, cards --
def person(pid):
    return _get(f"/persons/{pid}")


def person_photos(pid, limit=48, offset=0):
    """-> (photos [{image_id, photo_date, ...}] newest first, total)"""
    d = _get(f"/persons/{pid}/photos?limit={limit}&offset={offset}")
    return d.get("photos", []), d.get("total", 0)


def photo(image_id):
    return _get(f"/photo/{image_id}")


def _post(path, body, timeout=30):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _text_search(query, person_ids, date_from, date_to, top_k, area=None):
    # as photolib's own search page does: the model writes the English CLIP
    # prompt, all models fused (RRF); a single model with people filtered
    # returned too few (10 of top_k 30) even after photolib's fix, 2026-11-19
    body = {"query": query or "", "top_k": top_k, "translate": False, "multi_model": bool(query)}
    if person_ids:
        body.update(person_ids=person_ids, person_mode="and")
    if date_from:
        body["date_from"] = date_from
    if date_to:
        body["date_to"] = date_to
    if area:
        body.update(area)
    return _post("/search/text", body).get("results", [])


DOC_TAGS = profile.DOCUMENT_TAGS      # photolib tags whose photos are documents: hidden, private (eva/profile.json)


def documents(query="", person_ids=None, want=33):
    """Photographed documents (hidden, tagged): by what they look like, and
    whose face is on them. No OCR in photolib - the number is read by looking."""
    seen, out = set(), []
    for tag in DOC_TAGS:
        body = {"query": query or "", "top_k": 200, "translate": False, "multi_model": bool(query),
                "tag_ids": [tag], "include_hidden": True}
        if person_ids:
            body.update(person_ids=person_ids, person_mode="and")
        for p in _post("/search/text", body).get("results", []):
            if p["image_id"] not in seen:
                seen.add(p["image_id"])
                out.append(p)
    if query:
        out.sort(key=lambda p: p.get("similarity") or 0, reverse=True)
    else:
        out.sort(key=lambda p: p.get("photo_date") or "", reverse=True)
    return out[:want], len(out)


def search(query="", person_ids=None, date_from=None, date_to=None, area=None, want=66):
    """CLIP search (query = an English CLIP prompt) with filters: people,
    dates, an area {min_lat, max_lat, min_lon, max_lon}. -> (photos, total)
    Without a query: everything that passes the filters, spread over time."""
    if query:
        hits = _text_search(query, person_ids, date_from, date_to, 100, area)
        return hits[:want], len(hits)
    hits = _text_search("", person_ids, date_from, date_to, 20000, area)
    return spread(hits, want), len(hits)


EXIF_KEYS = ("Make", "Model", "LensModel", "FocalLength", "FNumber", "ExposureTime", "ISOSpeedRatings")


def meta(image_id):
    """What is known about one photo, for Eva (the same facts photolib's own
    photo assistant gets): date, people on it, GPS, camera, tags, folder."""
    p = photo(image_id)
    m = {"id": image_id}
    if p.get("photo_date"):
        m["date"] = p["photo_date"][:16].replace("T", " ")
    people = photo_people(image_id)
    try:
        faces = _get(f"/photo/{image_id}/faces")
        faces = faces.get("faces", faces) if isinstance(faces, dict) else faces
        unknown = sum(1 for f in faces or [] if not (f.get("person_name") or (f.get("person") or {}).get("name")))
    except (OSError, ValueError):
        unknown = 0
    if people:
        m["people"] = people
    if unknown:
        m["faces_unknown"] = unknown
    where = place(p)
    if where:
        m["gps"] = where
    exif = p.get("exif_data") or {}
    cam = {k: exif.get(k) or exif.get(f"EXIF {k}") for k in EXIF_KEYS}
    cam = {k: v for k, v in cam.items() if v and str(v).strip()}
    if cam:
        m["camera"] = cam
    tags = [t["name"] for t in p.get("tags") or [] if not t.get("is_system")]
    if tags:
        m["tags"] = tags
    if p.get("width") and p.get("height"):
        m["size"] = f"{p['width']}x{p['height']}"
    folder = (p.get("file_path") or "").split("/")
    if len(folder) > 2:
        m["folder"] = "/".join(folder[2:-1])[:80]
    return m


def metas(ids):
    """meta() of several photos at once (in parallel; a failed one is just its id)."""
    def one(i):
        try:
            return meta(i)
        except (OSError, ValueError, KeyError):
            return {"id": i}
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, ids))


def image(image_id, size=800):
    """JPEG bytes of a photo: a preview up to 800 px, or the full 2000 px (size=0)."""
    path = f"/image/{int(image_id)}/full" if not size else f"/image/{int(image_id)}/thumb?size={min(800, size)}"
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return r.read()


FULL_CHAIN = {"models": ["SigLIP", "ViT-L/14", "ViT-B/16", "ViT-B/32"], "include_faces": True,
              "include_phash": True, "include_cache_warm": True, "cache_warm_heavy_only": True}


def indexing(action):
    """photolib's admin "Index All" queue - the full chain with the admin page's
    default boxes ticked (all CLIP models -> faces -> pHash -> cache of heavy
    formats). action: start | status | stop -> the status dict (or {"error"})."""
    try:
        if action == "start":
            _post("/admin/index-all", FULL_CHAIN, timeout=30)
        elif action == "stop":
            _post("/admin/index-all/stop", {}, timeout=30)
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read()).get("detail", str(e))}
        except ValueError:
            return {"error": str(e)}
    except OSError as e:
        return {"error": str(e)}
    with urllib.request.urlopen(API + "/admin/index-all/status", timeout=15) as r:
        return json.loads(r.read())


def geocode(place, radius_km):
    """A place name -> an area around it (photolib /geo/geocode: Nominatim). -> (area, display) | (None, None)"""
    try:
        g = _post("/geo/geocode", {"query": place}, timeout=15)
    except (OSError, ValueError):
        return None, None
    lat, lon = g.get("latitude"), g.get("longitude")
    if lat is None or lon is None:
        return None, None
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return ({"min_lat": lat - dlat, "max_lat": lat + dlat, "min_lon": lon - dlon, "max_lon": lon + dlon},
            g.get("display") or place)


def similar(image_id, want=33):
    """Photos that look like this one (photolib /search/similar). -> [photo]"""
    r = _get(f"/search/similar/{int(image_id)}?top_k={want + 1}")
    return [p for p in r.get("results", []) if p.get("image_id") != image_id][:want]


def timeline(date_from, date_to, want=66):
    """Photos of a period, spread over it, oldest first. -> (photos, total)"""
    q = f"date_from={date_from}&date_to={date_to}"
    total = _get(f"/timeline/photos?limit=1&{q}").get("total", 0)
    got = {}
    for i in range(min(5, (total + 199) // 200)):
        off = i * total // 5 if total > 1000 else i * 200
        for ph in _get(f"/timeline/photos?limit=200&offset={off}&{q}").get("photos", []):
            got[ph["image_id"]] = ph
    return spread(list(got.values()), want), total


def spread(photos, want=66):
    """Oldest first, bursts thinned (a photo less than a minute after the last
    kept one is skipped), then evenly sampled down to `want`."""
    photos = sorted((p for p in photos if p.get("photo_date")), key=lambda p: p["photo_date"])
    kept, last = [], None
    for p in photos:
        t = datetime.datetime.fromisoformat(p["photo_date"][:19])
        if last is None or (t - last).total_seconds() >= 60:
            kept.append(p)
            last = t
    if len(kept) > want:
        kept = [kept[i * len(kept) // want] for i in range(want)]
    return kept


def place(ph):
    """'54.76, 17.49' or None: coordinates as they are (no geocoder at home);
    the junk ones (0,0 and the poles) left out."""
    lat, lon = ph.get("latitude"), ph.get("longitude")
    if lat is None or lon is None or (abs(lat) < 0.01 and abs(lon) < 0.01) or abs(lat) > 75:
        return None
    return f"{lat:.3f}, {lon:.3f}"


def photo_people(image_id):
    """Names of the recognised people on a photo."""
    try:
        faces = _get(f"/photo/{image_id}/faces")
    except (OSError, ValueError):
        return []
    faces = faces.get("faces", faces) if isinstance(faces, dict) else faces
    names = []
    for f in faces or []:
        n = f.get("person_name") or (f.get("person") or {}).get("name")
        if n and n not in names:
            names.append(n)
    return names
