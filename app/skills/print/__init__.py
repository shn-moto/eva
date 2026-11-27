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
_AGAIN = {"type": "boolean", "description": "print the same again (only if the person plainly asks to repeat)"}
_PAGES = {"type": "string", "description": "pages, e.g. 1-3,5; empty - all"}

SKILL = Skill(
    name="print",
    summary="присланные в Телеграм файлы и фото (посмотреть, разобрать) и печать на домашнем принтере",
    prompt="""## Files sent to you and printing

What is sent to you in Telegram (photos, pictures, PDF, office documents - odt, ods,
docx, xlsx..., other files) is kept under a file_id - in the chat a mark «[прислан
файл: …, file_id=…]».
- Asked about a sent file («что это за растение», «что тут написано», «посчитай по
  чеку») - file_view first, then answer. No caption - look, say briefly what it is,
  ask what to do with it.
- The printer at home is one: laser, black and white, A4. Anything you showed or
  computed can be printed. A web page is not printed whole - retell it and
  print_text. print_page - the same as show_html but for paper (white background,
  black text and lines, A4, layout=document).
- «Распечатай это», «распечатай» with nothing more - what is on the TV:
  print_screen. Your own drawing - file_print with its file_id. Do not take photo
  numbers from old answers unless it is clear they are meant: unclear what to print -
  ask (an old photo of Соня once went instead of a coloring page).
- Asked to print - print without asking; say how many pages went. Over 10 pages or
  copies - check first. Photos come out black and white.""",
    tools=[Tool(
        name="file_view",
        description=("Look at a sent file by file_id: a picture as a picture, PDF as a document, text as text. "
                     "detail=true - a picture at full size (small text)."),
        schema={"type": "object", "additionalProperties": False, "required": ["file_id"],
                "properties": {"file_id": {"type": "string"}, "detail": {"type": "boolean"}}},
        handler=view, needs="known",
    ), Tool(
        name="file_print",
        description="Print a sent file (PDF, picture, office document) on the home printer.",
        schema={"type": "object", "additionalProperties": False, "required": ["file_id"],
                "properties": {"again": _AGAIN, "file_id": {"type": "string"}, "copies": _COPIES, "pages": _PAGES,
                               "fit": {"type": "string", "enum": ["fit", "fill"],
                                       "description": "picture: fit - whole with margins, fill - the whole sheet, cropped"}}},
        handler=print_file, needs="known",
    ), Tool(
        name="print_photos",
        description="Print archive photos (numbers from a photo search), each on its own A4 sheet.",
        schema={"type": "object", "additionalProperties": False, "required": ["photo_ids"],
                "properties": {"again": _AGAIN, "photo_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 20},
                               "copies": _COPIES, "fit": {"type": "string", "enum": ["fit", "fill"]}}},
        handler=print_photos, needs="known",
    ), Tool(
        name="print_text",
        description=("Print a text you wrote (a list, recipe, note). Lines «# » - headings, «- » and «1. » - "
                     "items."),
        schema={"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"again": _AGAIN, "title": {"type": "string"}, "text": {"type": "string"}, "copies": _COPIES}},
        handler=print_text, needs="known",
    ), Tool(
        name="print_url",
        description="Print a PDF or picture by link (from a web search). Web pages are not printed.",
        schema={"type": "object", "additionalProperties": False, "required": ["url"],
                "properties": {"again": _AGAIN, "url": {"type": "string"}, "copies": _COPIES, "pages": _PAGES}},
        handler=print_url, needs="known",
    ), Tool(
        name="print_page",
        description=("Print your own page: a chart, report, table (finances, electricity, anything). html - a "
                     "whole document for paper: white background, black text, chart lines dark and "
                     "distinguishable without colour (the printer is black and white), font 12-14 pt. Charts: "
                     '<script src="/lib/chart.umd.min.js"></script> (Chart.js 4), animation: false. layout: '
                     "document - A4 pages as a browser prints (default); screen - one 1280x720 picture per "
                     "sheet, as on the TV."),
        schema={"type": "object", "additionalProperties": False, "required": ["html"],
                "properties": {"again": _AGAIN, "title": {"type": "string"}, "html": {"type": "string"},
                               "layout": {"type": "string", "enum": ["document", "screen"]}, "copies": _COPIES}},
        handler=print_page, needs="known",
    ), Tool(
        name="print_screen",
        description=("Print what is on the TV now: the open photo, the slideshow photo, your drawing, your "
                     "page (as on screen; a dark background prints light). A grid of several photos - only "
                     "with all=true (each on its own sheet)."),
        schema={"type": "object", "additionalProperties": False,
                "properties": {"again": _AGAIN, "copies": _COPIES, "all": {"type": "boolean"},
                               "fit": {"type": "string", "enum": ["fit", "fill"]}}},
        handler=print_screen, needs="screen",
    ), Tool(
        name="printer_status",
        description="The home printer: is it ready, what is in the queue.",
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=status, needs="known",
    ), Tool(
        name="print_cancel",
        description="Cancel a print job (number from printer_status or from a print answer).",
        schema={"type": "object", "additionalProperties": False, "required": ["job"],
                "properties": {"job": {"type": "string"}}},
        handler=cancel, needs="known",
    )],
)
