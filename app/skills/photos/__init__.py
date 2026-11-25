"""Skill "фото": the family photo archive in photolib (~150k photos) - search
by description, people and dates, a period as a feed, what a photo on the screen
is. Results go to the TV (voice) or as an album (Telegram)."""
import base64
import json

import photolib
from core import screen, skills
from core.skills import Result, Skill, Tool


def _d(iso):
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if iso and len(iso) >= 10 else ""


def _people(names):
    """names -> (person_ids, error text)"""
    ids = []
    for n in names or []:
        hits = photolib.find(n)
        if not hits:
            return None, f"В семье нет человека «{n}»."
        ids.append(hits[0])
    return ids, None


FIRST = 11                     # the first page on the TV (and more than a Telegram album of 10)


SHOWN = {}                     # (channel, person) -> image ids of the last presentation, by number


def remember(person, items):
    """What was shown last on this channel: «третье фото» in Telegram means the
    album, not the TV (the TV is screen.state)."""
    SHOWN[(person.channel, person.person_id)] = [it["id"] for it in items]


def with_meta(text, items):
    """The facts about the first photos go to Eva as JSON (who is on them,
    when, where, camera) - she knows what she shows without looking; the
    people go into the captions too (Telegram)."""
    ms = photolib.metas([it["id"] for it in items[:FIRST]])
    for it, m in zip(items, ms):
        if m.get("people"):
            it["people"] = m["people"]
        if m.get("gps"):
            it["gps"] = m["gps"]
    lines = "\n".join(json.dumps(dict(m, n=k + 1), ensure_ascii=False) for k, m in enumerate(ms))
    return f"{text}\nПервые {len(ms)} (n - номер на экране / в альбоме), метаданные из архива:\n{lines}"


def _span(photos):
    dates = sorted(p["photo_date"][:10] for p in photos if p.get("photo_date"))
    if not dates:
        return "даты не известны"
    return _d(dates[0]) if dates[0] == dates[-1] else f"с {_d(dates[0])} по {_d(dates[-1])}"


def search(args, person):
    ids, err = _people(args.get("people"))
    if err:
        return err
    query = (args.get("query") or "").strip()
    area, where = None, ""
    if args.get("place"):
        area, where = photolib.geocode(args["place"], float(args.get("radius_km") or 20))
        if not area:
            return f"Не нашла на карте место «{args['place']}»."
    if not query and not ids and not args.get("date_from") and not area:
        return "Нужно хоть что-то: описание, люди, даты или место."
    try:
        photos, total = photolib.search(query, ids, args.get("date_from"), args.get("date_to"), area)
    except OSError as e:
        return f"Фотоархив недоступен: {e}"
    if not photos:
        return "Ничего не нашлось."
    items = [{"id": p["image_id"], "caption": _d(p.get("photo_date") or "")} for p in photos]
    persons, _ = photolib.tree()
    who = ", ".join(persons[i]["name"] for i in ids or [] if i in persons)
    title = (args.get("title") or "").strip() or " · ".join(x for x in (who, args.get("place")) if x) or query or "фото"
    places = sorted({photolib.place(p) for p in photos[:12]} - {None})
    if query:   # a ranking has no "found": the top is shown, the tail gets weaker
        count = f"Показала {len(items)} самых похожих фото «{title}» (первые - лучшие, дальше бывают промахи)"
    else:
        count = f"Показала {len(items)} фото «{title}» из {total}, по порядку, равномерно по времени"
    text = (f"{count}, {_span(photos)}"
            + (f"; место: {where}, в радиусе {args.get('radius_km') or 20} км (фото без координат сюда не "
               f"попадают)" if where else "")
            + (f"; координаты первых: {'; '.join(places[:4])}" if places and not where else "") + ".")
    remember(person, items)
    return Result(with_meta(text, items), {"kind": "photos", "title": title, "items": items})


def feed(args, person):
    try:
        photos, total = photolib.timeline(args["date_from"], args["date_to"])
    except OSError as e:
        return f"Фотоархив недоступен: {e}"
    if not photos:
        return "За этот период фото нет."
    items = [{"id": p["image_id"], "caption": _d(p["photo_date"])} for p in photos]
    title = f"{_d(args['date_from'])} - {_d(args['date_to'])}"
    remember(person, items)
    return Result(with_meta(f"Показала ленту: {len(items)} фото из {total} за период, по порядку, {_span(photos)}.", items),
                  {"kind": "photos", "title": title, "items": items})


def like(args, person):
    """Photos that look like one on the screen."""
    image_id = _which(args, person)
    if not image_id:
        return "Не понятно, на какое фото похожие: назови номер на экране."
    try:
        photos = photolib.similar(image_id)
    except OSError as e:
        return f"Фотоархив недоступен: {e}"
    if not photos:
        return "Похожих не нашлось."
    items = [{"id": image_id, "caption": "это фото"}] + [
        {"id": p["image_id"], "caption": _d(p.get("photo_date") or "")} for p in photos]
    remember(person, items)
    return Result(with_meta(f"Показала {len(photos)} фото, похожих на #{image_id} (первым - оно само), {_span(photos)}.", items),
                  {"kind": "photos", "title": "похожие фото", "items": items})


def index(args, person):
    """photolib's full indexing chain (admin page "Index All")."""
    action = args.get("action", "status")
    st = photolib.indexing(action)
    if st.get("error"):
        return f"Не получилось ({action}): {st['error']}"
    if not st.get("running"):
        if action == "start":
            return "Индексация не запустилась, photolib не сообщил почему."
        done = f", закончилась {st['finished_at'][:16].replace('T', ' ')}" if st.get("finished_at") else ""
        return f"Индексация сейчас не идёт{done}" + (f"; ошибка: {st['error']}" if st.get("error") else "") + "."
    sub = st.get("sub_progress") or {}
    pct = sub.get("percentage")
    eta = sub.get("eta_seconds")
    parts = [("Запустила" if action == "start" else "Идёт") + " полную индексацию в photolib",
             "очередь: " + ", ".join(st.get("queue") or []),
             "сейчас: " + str(st.get("current_task")) + (f" ({pct}%)" if pct is not None else "")
             + (f", осталось ~{int(eta // 60)} мин" if eta else "")]
    if st.get("completed"):
        parts.append("готово: " + ", ".join(st["completed"]))
    if action == "stop":
        parts[0] = "Остановка запрошена: текущая задача доработает, остальные отменены"
    return "; ".join(parts) + "."


def docs(args, person):
    """Photographed documents (passports, IDs, certificates): hidden in the
    archive, for adults at home only."""
    ids, err = _people(args.get("people"))
    if err:
        return err
    try:
        photos, total = photolib.documents((args.get("query") or "").strip(), ids)
    except OSError as e:
        return f"Фотоархив недоступен: {e}"
    if not photos:
        return "Таких документов в архиве не нашлось."
    items = [{"id": p["image_id"], "caption": _d(p.get("photo_date") or "")} for p in photos]
    remember(person, items)
    return Result(with_meta(f"Нашла {total} фото документов, показала {len(items)}. Текст на них не распознан: "
                            f"чтобы прочитать номер или данные - посмотри фото (photo_look с id, detail=true).",
                            items), {"kind": "photos", "title": args.get("title") or "документы", "items": items})


def _which(args, person):
    """The photo meant: an id from the metadata, or number n of what was shown
    last on this channel (the TV for the voice, the album for Telegram)."""
    if args.get("id"):
        return int(args["id"])
    n = args.get("n")
    n = int(n) if isinstance(n, (int, float)) else None
    if person.channel == "voice":
        return screen.item(n)
    ids = SHOWN.get((person.channel, person.person_id)) or []
    return ids[n - 1] if n and 1 <= n <= len(ids) else None


def info(args, person):
    image_id = _which(args, person)
    if not image_id:
        return "Не понятно, какое фото: назови номер на экране."
    m = photolib.meta(image_id)
    return Result("Метаданные фото из архива (gps - назови место, если узнаёшь): "
                  + json.dumps(m, ensure_ascii=False), show=_pin(image_id, person))


def _pin(image_id, person):
    """Asked about the slide on the TV: it stays there, as a photo view (the
    slideshow had held it while she answered)."""
    import slideshow
    h = slideshow.held_photo()
    if person.channel != "voice" or not h or h.get("id") != image_id:
        return None
    slideshow.release_held()
    return {"kind": "photo", "id": image_id, "caption": h.get("title", "")}


def look(args, person):
    """The photo itself for her eyes (like photolib's photo assistant): a
    preview of 800 px, or the full 2000 px when details matter."""
    image_id = _which(args, person)
    if not image_id:
        return "Не понятно, какое фото: назови номер на экране."
    pin = _pin(image_id, person)
    jpeg = photolib.image(image_id, size=0 if args.get("detail") else 800)
    m = photolib.meta(image_id)
    data = base64.b64encode(jpeg).decode("ascii")
    try:      # the history keeps a 512 px copy: she remembers she really saw it
        skills.HISTORY_IMAGES[data] = base64.b64encode(photolib.image(image_id, size=512)).decode("ascii")
    except OSError:
        pass
    return Result("", show=pin, blocks=[
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": "Это фото #" + str(image_id) + ". Метаданные из архива: "
                                 + json.dumps(m, ensure_ascii=False)}])


PROMPT = """## Фотоархив

Семейный архив в photolib - около 150 тысяч фото. photo_search ищет по смыслу
картинки («на море», «Соня с тортом»), по людям, датам и месту съёмки
(«фото из Устки», «что снимали в Кракове»); similar_photos - похожие на фото
с экрана; show_feed - лента
за период («что было летом 2024»); photo_info - метаданные фото с экрана, photo_look -
посмотреть на само фото (когда просят описать / разобрать картинку). К результатам
поиска приходят метаданные первых фото: кто на них, когда, где, - отвечай по ним,
не открывая картинку. Найденное появляется на телевизоре (в Телеграме - альбомом).
Говори, что нашла, одной-двумя фразами; не перечисляй фото по одному.
Кто на фото: people - люди, подписанные в архиве. faces_like - лица, которые
в архиве ещё не подписаны, но похожи на подписанных (сходство и сколько фото
совпало): от 0.7 и несколько совпадений - это почти наверняка он(а), так и
говори («это Аврора»); 0.6-0.7 - «похоже, это …». Своему впечатлению от
картинки не верь больше, чем лицам архива: сёстры похожи, и по глазу ты их
путала."""

# the rules of photolib's own search assistant (its /ai/search-assistant prompt)
CLIP = ("что на фото - промпт для CLIP ПО-АНГЛИЙСКИ, 5-15 слов, только то, что видит камера: предметы, "
        "цвета, обстановка, свет («sandy sea beach with waves», «birthday cake with lit candles», "
        "«child riding a bicycle in a park»). Без имён (люди - в people, вместо имени «a child», «a woman»), "
        "без названий мест (место - в place), без дат. Конкретное место внутри города (каменоломня, озеро, "
        "парк) - place города + короткое описание вида: «rocky stone quarry pit».")
_PEOPLE = {"type": "array", "items": {"type": "string"}, "description": "имена людей, все должны быть на фото"}
_DATE = {"type": "string", "description": "YYYY-MM-DD"}

SKILL = Skill(
    name="photos",
    summary="семейный фотоархив: поиск, лента за период, что на фото",
    prompt=PROMPT,
    tools=[Tool(
        name="photo_search",
        description="Найти фото в семейном архиве и показать их: по описанию того, что на картинке "
                    "(query), по людям на фото, по датам, по месту съёмки (place + radius_km). Любое сочетание; без описания - просто "
                    "фото этих людей / за эти даты, по порядку.",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"query": {"type": "string", "description": CLIP},
                               "people": _PEOPLE, "date_from": _DATE, "date_to": _DATE,
                               "place": {"type": "string", "description": "место съёмки: город, курорт, "
                                         "страна - «Устка», «Краков», «Черногория»"},
                               "radius_km": {"type": "number", "description": "радиус вокруг места, км: посёлок ~3-5, "
                                             "город ~10-15, область/побережье ~50-150, страна - по её размеру"},
                               "title": {"type": "string", "description": "подпись над фото на экране, "
                                         "по-русски, 2-4 слова: «Соня на море»"}}},
        handler=search,
        needs="search.text",
    ), Tool(
        name="show_feed",
        description="Показать ленту фото за период по порядку (серии подряд прорежены), до 66 фото, "
                    "равномерно по периоду. Для «что было в июле», «покажи прошлое лето».",
        schema={"type": "object", "additionalProperties": False, "required": ["date_from", "date_to"],
                "properties": {"date_from": _DATE, "date_to": _DATE}},
        handler=feed,
        needs="photos.view",
    ), Tool(
        name="similar_photos",
        description="Показать фото, похожие на фото номер n на экране (или на открытое сейчас): та же "
                    "сцена, место, событие.",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"n": {"type": "integer", "description": "номер фото на экране / в альбоме"},
                               "id": {"type": "integer", "description": "id фото из метаданных (вместо n)"}}},
        handler=like,
        needs="search.image",
    ), Tool(
        name="photo_indexing",
        description="Индексация фотоархива в photolib (полная цепочка, как кнопка в админке: все CLIP-модели, "
                    "лица, pHash, кэш): start - запустить («запусти индексацию»), status - как идёт, stop - "
                    "остановить. Индексация долгая (часы) и грузит сервер.",
        schema={"type": "object", "additionalProperties": False, "required": ["action"],
                "properties": {"action": {"type": "string", "enum": ["start", "status", "stop"]}}},
        handler=index,
        needs="admin",
    ), Tool(
        name="document_search",
        description="Найти сфотографированные документы (паспорт, удостоверение, свидетельство, полис, "
                    "справки) в скрытом разделе архива. query - как документ выглядит, по-английски («Polish ID "
                    "card», «passport page», «birth certificate»), people - чей. Номера и текст прочитай, "
                    "посмотрев фото (photo_look по id, detail=true).",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"query": {"type": "string"}, "people": _PEOPLE,
                               "title": {"type": "string", "description": "подпись на экране по-русски"}}},
        handler=docs,
        needs="private",
    ), Tool(
        name="photo_look",
        description="Посмотреть на само фото глазами (картинка + метаданные): что на нём, что происходит, "
                    "детали, текст на фото. n - номер на экране; без n - открытое сейчас. detail=true - в "
                    "полном размере (мелкие детали, надписи). Только когда просят разобрать / описать "
                    "картинку: кто на фото, когда и где - это уже есть в метаданных результата поиска.",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"n": {"type": "integer", "description": "номер фото на экране / в альбоме"},
                               "id": {"type": "integer", "description": "id фото из метаданных (вместо n)"},
                               "detail": {"type": "boolean"}}},
        handler=look,
        needs="photos.view",
    ), Tool(
        name="photo_info",
        description="Что за фото: когда снято, кто на нём, где (координаты), папка в архиве. n - номер "
                    "на экране; без n - открытое сейчас фото.",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"n": {"type": "integer", "description": "номер фото на экране / в альбоме"},
                               "id": {"type": "integer", "description": "id фото из метаданных (вместо n)"}}},
        handler=info,
        needs="photos.view",
    )],
)
