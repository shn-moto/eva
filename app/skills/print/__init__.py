"""Skill "файлы и печать": what people send her in Telegram (photos, PDFs, other
files), her eyes on them, and the home printer.

The files and the printing are on the hub (services/print): Telegram's files
go there straight away (the Station's disk is small), each with an id she sees
in the conversation as «[прислан файл: … file_id=…]». A print job is made a PDF
there and sent to the home's CUPS."""
import base64
import json
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
                skills.HISTORY_IMAGES[data] = base64.b64encode(_req(f"/files/{fid}/jpeg?size=200")).decode("ascii")
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


def _print(body):
    try:
        r = _req("/print", body, timeout=120)
    except Exception as e:
        return _err(e)
    pages = r.get("pages")
    return (f"Отправила на принтер: задание {r['job']}, "
            + (f"страниц {pages}, " if pages else "") + f"копий {r['copies']}.")


def print_file(args, person):
    return _print({"kind": "file", "file_id": args["file_id"], "copies": args.get("copies", 1),
                   "pages": args.get("pages") or None, "fit": args.get("fit")})


def print_photos(args, person):
    return _print({"kind": "photos", "photo_ids": args["photo_ids"], "copies": args.get("copies", 1),
                   "fit": args.get("fit")})


def print_text(args, person):
    return _print({"kind": "text", "title": args.get("title", ""), "text": args["text"],
                   "copies": args.get("copies", 1)})


def print_url(args, person):
    return _print({"kind": "url", "url": args["url"], "copies": args.get("copies", 1),
                   "pages": args.get("pages") or None})


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
  печатаешь - перескажи и напечатай текстом). printer_status - принтер и очередь,
  print_cancel - отменить задание.
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
                "properties": {"file_id": {"type": "string"}, "copies": _COPIES, "pages": _PAGES,
                               "fit": {"type": "string", "enum": ["fit", "fill"],
                                       "description": "картинка: fit - целиком с полями, fill - на весь лист с обрезкой"}}},
        handler=print_file, needs="known",
    ), Tool(
        name="print_photos",
        description="Напечатать фото из домашнего архива (номера из поиска фото), каждое на своём листе A4.",
        schema={"type": "object", "additionalProperties": False, "required": ["photo_ids"],
                "properties": {"photo_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 20},
                               "copies": _COPIES, "fit": {"type": "string", "enum": ["fit", "fill"]}}},
        handler=print_photos, needs="known",
    ), Tool(
        name="print_text",
        description="Напечатать текст, который ты составила (список, рецепт, заметка). Строки «# » - заголовки, "
                    "«- » и «1. » - пункты.",
        schema={"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"title": {"type": "string"}, "text": {"type": "string"}, "copies": _COPIES}},
        handler=print_text, needs="known",
    ), Tool(
        name="print_url",
        description="Напечатать PDF или картинку по ссылке (из поиска в интернете). Веб-страницы не печатаются.",
        schema={"type": "object", "additionalProperties": False, "required": ["url"],
                "properties": {"url": {"type": "string"}, "copies": _COPIES, "pages": _PAGES}},
        handler=print_url, needs="known",
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
