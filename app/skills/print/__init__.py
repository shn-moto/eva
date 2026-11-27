"""Skill "файлы и печать": what people send her in Telegram (photos, PDFs, other
files), her eyes on them, and the home printer.

The files and the printing are on the hub (services/print): Telegram's files
go there straight away (the Station's disk is small), each with an id she sees
in the conversation as «[прислан файл: … file_id=…]». A print job is made a PDF
there and sent to the home's CUPS."""
import base64
import hashlib
import json
import time
import urllib.error
import urllib.request

from core import profile, skills
from core.skills import Result, Skill, Tool

API = profile.service("print")


def _req(path, body=None, timeout=60):
    req = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return raw if r.headers.get_content_type() not in ("application/json",) else json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = str(e)
        raise RuntimeError(detail)


def _err(e):
    return f"Не вышло: {e}"


def view(args, person):
    """The file for her eyes: a picture as a picture, a PDF as a document."""
    fid = args["file_id"]
    try:
        meta = _req(f"/files/{fid}/meta")
        if meta["kind"] == "image":
            jpeg = _req(f"/files/{fid}/jpeg?size={1568 if args.get('detail') else 1024}")
            data = base64.b64encode(jpeg).decode("ascii")
            try:      # the history keeps a small copy: she remembers she saw it
                skills.HISTORY_IMAGES[data] = base64.b64encode(_req(f"/files/{fid}/jpeg?size=512")).decode("ascii")
            except Exception:
                pass
            return Result("", blocks=[
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                {"type": "text", "text": f"Файл «{meta['name']}» (file_id {fid})."}])
        if meta["kind"] == "office" and not meta.get("pdf"):
            return f"«{meta['name']}» не открылся: {meta.get('error') or 'документ не перевёлся в PDF'}."
        if meta["kind"] in ("pdf", "office"):
            if meta["size"] > 20 * 1024 * 1024:
                return f"«{meta['name']}» - PDF больше 20 МБ, открыть не могу; напечатать - могу."
            pdf = _req(f"/files/{fid}/raw", timeout=90)
            return Result("", blocks=[
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(pdf).decode("ascii")}},
                {"type": "text", "text": f"Файл «{meta['name']}» (file_id {fid}), страниц: {meta.get('pages') or '?'}"
                                         + (" - офисный документ, показан как PDF." if meta["kind"] == "office" else ".")}])
        if (meta.get("mime") or "").startswith("text/") or meta["name"].lower().endswith((".txt", ".csv", ".md")):
            text = _req(f"/files/{fid}/raw").decode("utf-8", "replace")
            return f"Файл «{meta['name']}»:\n{text[:20000]}"
        return (f"«{meta['name']}» ({meta.get('mime') or 'неизвестный тип'}) открыть не умею: картинки, PDF, "
                "офисные документы (odt, ods, odp, docx, xlsx, pptx, doc, rtf...) и текст.")
    except Exception as e:
        return _err(e)


# the same thing sent again within this many seconds is not printed: she once took her own
# «Печатаю.» for an empty promise and printed the page twice (2026-11-26); again=true when
# the person really asks for one more
REPEAT_S = 180
RECENT = {}                                 # key -> (time, the answer of that job)


def _key(body):
    kind = body.get("kind")
    what = {"file": body.get("file_id"), "photos": tuple(body.get("photo_ids") or ()), "url": body.get("url"),
            "text": hashlib.sha1((body.get("text") or "").encode()).hexdigest(),
            "html": hashlib.sha1((body.get("html") or "").encode()).hexdigest()}.get(kind)
    return f"{kind}:{what}"


def _print(args, body):
    key, now = _key(body), time.time()
    last = RECENT.get(key)
    if last and now - last[0] < REPEAT_S and not args.get("again"):
        return (f"Это уже ушло на принтер {int(now - last[0])} с назад ({last[1]}) - второй раз не отправляю. "
                "Ещё раз - только если человек прямо просит повторить: тогда again=true.")
    try:
        r = _req("/print", body, timeout=120)
    except Exception as e:
        return _err(e)
    pages = r.get("pages")
    out = (f"Отправила на принтер: задание {r['job']}, "
           + (f"страниц {pages}, " if pages else "") + f"копий {r['copies']}.")
    RECENT[key] = (now, out)
    return out


def print_file(args, person):
    return _print(args, {"kind": "file", "file_id": args["file_id"], "copies": args.get("copies", 1),
                   "pages": args.get("pages") or None, "fit": args.get("fit")})


def print_photos(args, person):
    return _print(args, {"kind": "photos", "photo_ids": args["photo_ids"], "copies": args.get("copies", 1),
                   "fit": args.get("fit")})


def print_text(args, person):
    return _print(args, {"kind": "text", "title": args.get("title", ""), "text": args["text"],
                   "copies": args.get("copies", 1)})


def print_url(args, person):
    return _print(args, {"kind": "url", "url": args["url"], "copies": args.get("copies", 1),
                   "pages": args.get("pages") or None})


def print_page(args, person):
    return _print(args, {"kind": "html", "title": args.get("title", ""), "html": args["html"],
                   "layout": args.get("layout") or "document", "copies": args.get("copies", 1)})


def print_screen(args, person):
    """Whatever the TV shows now: a photo (open, the slideshow's), a page of
    photos, a picture she drew, a page she wrote (as the TV shows it)."""
    from core import screen
    s = screen.shown()
    copies = args.get("copies", 1)
    if not s:
        return "На экране сейчас ничего для печати (только лицо или плеер)."
    if s.get("photo"):
        return _print(args, {"kind": "photos", "photo_ids": [s["photo"]], "copies": copies, "fit": args.get("fit")})
    if s.get("file"):
        return _print(args, {"kind": "file", "file_id": s["file"], "copies": copies, "fit": args.get("fit"),
                       "title": s.get("title")})
    if s.get("html"):
        return _print(args, {"kind": "html", "html": s["html"], "title": s.get("title", ""), "layout": "screen",
                       "copies": copies})
    if s.get("photos"):
        if len(s["photos"]) > 1 and not args.get("all"):
            return (f"На экране {len(s['photos'])} фото: каждое печатается на своём листе. Уточни, какие "
                    "(print_photos по номерам) или все (print_screen all=true).")
        return _print(args, {"kind": "photos", "photo_ids": s["photos"], "copies": copies, "fit": args.get("fit")})
    return "Не поняла, что на экране печатать."


def status(args, person):
    try:
        return json.dumps(_req("/printers"), ensure_ascii=False)
    except Exception as e:
        return _err(e)


def cancel(args, person):
    try:
        _req("/cancel", {"job": args["job"]})
        return f"Задание {args['job']} отменено."
    except Exception as e:
        return _err(e)


_COPIES = {"type": "integer", "minimum": 1, "maximum": 5}
_AGAIN = {"type": "boolean", "description": "печатать то же самое ещё раз (только если человек прямо просит повторить)"}
_PAGES = {"type": "string", "description": "страницы, например 1-3,5; пусто - все"}

SKILL = Skill(
    name="print",
    summary="присланные в Телеграм файлы и фото (посмотреть, разобрать) и печать на домашнем принтере",
    prompt="""## Присланные файлы и печать

Что присылают тебе в Телеграме (фото, картинки, PDF, офисные документы - odt, ods,
docx, xlsx и др., другие файлы), лежит у тебя
под file_id - в переписке это пометка «[прислан файл: …, file_id=…]».
- file_view - посмотреть: картинку видишь, PDF читаешь. Спрашивают про присланное
  («что это за растение», «что тут написано», «посчитай по чеку») - сначала
  посмотри, потом отвечай. Без подписи к файлу - посмотри и коротко скажи, что это,
  спроси, что с ним сделать.
- Печать (принтер дома один - лазерный, чёрно-белый, A4): file_print - присланный
  файл (PDF, картинка); print_photos - фото из архива по номерам; print_text -
  то, что ты составила сама (список покупок, рецепт, заметка: заголовки «# »,
  пункты «- »); print_url - PDF или картинка по ссылке (веб-страницу целиком не
  печатаешь - перескажи и напечатай текстом); print_page - своя страница
  (график, отчёт, таблица по финансам, электроэнергии, чему угодно - то же,
  что для show_html, но для бумаги: белый фон, чёрный текст и линии, A4;
  layout=document); print_screen - то, что сейчас на телевизоре («напечатай
  это»: открытое фото, фото заставки, твой рисунок, твоя страница).
  Печать - для любого вывода: что показала или посчитала, можно напечатать. printer_status - принтер и очередь,
  print_cancel - отменить задание.
- «Распечатай это», «распечатай» без уточнения - то, что на телевизоре:
  print_screen. Свой рисунок - file_print с его file_id. Номера фото из старых
  ответов для печати не бери, если не ясно, что о них речь: непонятно, что
  печатать, - переспроси (вместо раскраски однажды ушло старое фото Сони).
- Просят напечатать - печатай, не переспрашивая; скажи, сколько страниц ушло.
  Больше 10 страниц или копий - сначала уточни. Фото выйдут чёрно-белыми.""",
    tools=[Tool(
        name="file_view",
        description="Посмотреть присланный файл по file_id: картинка видна как картинка, PDF - как документ, "
                    "текстовый файл - текстом. detail=true - картинка в полном размере (мелкий текст).",
        schema={"type": "object", "additionalProperties": False, "required": ["file_id"],
                "properties": {"file_id": {"type": "string"}, "detail": {"type": "boolean"}}},
        handler=view, needs="known",
    ), Tool(
        name="file_print",
        description="Напечатать присланный файл (PDF, картинку, офисный документ) на домашнем принтере.",
        schema={"type": "object", "additionalProperties": False, "required": ["file_id"],
                "properties": {"again": _AGAIN, "file_id": {"type": "string"}, "copies": _COPIES, "pages": _PAGES,
                               "fit": {"type": "string", "enum": ["fit", "fill"],
                                       "description": "картинка: fit - целиком с полями, fill - на весь лист с обрезкой"}}},
        handler=print_file, needs="known",
    ), Tool(
        name="print_photos",
        description="Напечатать фото из домашнего архива (номера из поиска фото), каждое на своём листе A4.",
        schema={"type": "object", "additionalProperties": False, "required": ["photo_ids"],
                "properties": {"again": _AGAIN, "photo_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 20},
                               "copies": _COPIES, "fit": {"type": "string", "enum": ["fit", "fill"]}}},
        handler=print_photos, needs="known",
    ), Tool(
        name="print_text",
        description="Напечатать текст, который ты составила (список, рецепт, заметка). Строки «# » - заголовки, "
                    "«- » и «1. » - пункты.",
        schema={"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"again": _AGAIN, "title": {"type": "string"}, "text": {"type": "string"}, "copies": _COPIES}},
        handler=print_text, needs="known",
    ), Tool(
        name="print_url",
        description="Напечатать PDF или картинку по ссылке (из поиска в интернете). Веб-страницы не печатаются.",
        schema={"type": "object", "additionalProperties": False, "required": ["url"],
                "properties": {"again": _AGAIN, "url": {"type": "string"}, "copies": _COPIES, "pages": _PAGES}},
        handler=print_url, needs="known",
    ), Tool(
        name="print_page",
        description="Напечатать свою страницу: график, отчёт, таблицу (финансы, электроэнергия, что угодно). "
                    "html - документ целиком, для бумаги: белый фон, чёрный текст, линии графиков тёмные и "
                    "различимые без цвета (принтер чёрно-белый), шрифт 12-14 pt. Графики: "
                    "<script src=\"/lib/chart.umd.min.js\"></script> (Chart.js 4), animation: false. "
                    "layout: document - A4 страницы, как печатает браузер (по умолчанию); screen - одна "
                    "картинка 1280x720 на лист, как на телевизоре.",
        schema={"type": "object", "additionalProperties": False, "required": ["html"],
                "properties": {"again": _AGAIN, "title": {"type": "string"}, "html": {"type": "string"},
                               "layout": {"type": "string", "enum": ["document", "screen"]}, "copies": _COPIES}},
        handler=print_page, needs="known",
    ), Tool(
        name="print_screen",
        description="Напечатать то, что сейчас на телевизоре: открытое фото, фото заставки, твой рисунок, "
                    "твою страницу (как на экране; тёмный фон печатается светлым). Сетка из нескольких фото - "
                    "только с all=true (каждое на своём листе).",
        schema={"type": "object", "additionalProperties": False,
                "properties": {"again": _AGAIN, "copies": _COPIES, "all": {"type": "boolean"},
                               "fit": {"type": "string", "enum": ["fit", "fill"]}}},
        handler=print_screen, needs="screen",
    ), Tool(
        name="printer_status",
        description="Домашний принтер: готов ли, что в очереди печати.",
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=status, needs="known",
    ), Tool(
        name="print_cancel",
        description="Отменить задание печати (номер из printer_status или из ответа печати).",
        schema={"type": "object", "additionalProperties": False, "required": ["job"],
                "properties": {"job": {"type": "string"}}},
        handler=cancel, needs="known",
    )],
)
