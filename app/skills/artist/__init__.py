"""Skill "художник": she draws - a picture from words, or a photo / a sent
picture / her own earlier picture redrawn - shows it (the TV, or a photo in
Telegram), keeps it, prints it on request and saves it to the family archive.

Everything about drawing is here, the pictures API included. Which API is
profile.json "artist" (every key optional, the defaults are SAM Grid):

    "artist": {
      "base_url":   "https://api.grid.sam-solutions.net/v1",   an OpenAI-compatible images API:
                    POST /images/generations {model, prompt, size, n}, POST /images/edits (multipart)
      "key":        "GRID_KEY",               the name of its key in /etc/eva/secrets.env
      "model":      "saipixel-qwen-image-2.1", the best picture (~30 s)
      "fast_model": "saipixel-krea2-turbo",    a quick one (~10 s)
      "edit_model": "saipixel-flux2-klein-9b-edit",  redraw a picture ("" - this API cannot)
      "sizes":      {"square": "1024x1024", "portrait": "832x1216", "landscape": "1216x832"},
      "album":      "Картинки от Евы",         the photolib album her saved pictures go to
      "folder":     "_EVA"                     their folder in the archive
    }

A picture lives in the print service's inbox (services/print /files, a week)
under a file_id: the TV shows it from there (ui_server /file/<id>), file_print
prints it, file_view looks at it again. Saved to photolib it is there for good.
"""
import base64
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import photolib
from core import profile, skills
from core.skills import Result, Skill, Tool
from core.util import log, secrets

CFG = {
    "base_url": "https://api.grid.sam-solutions.net/v1",
    "key": "GRID_KEY",
    "model": "saipixel-qwen-image-2.1",
    "fast_model": "saipixel-krea2-turbo",
    "edit_model": "saipixel-flux2-klein-9b-edit",
    "sizes": {"square": "1024x1024", "portrait": "832x1216", "landscape": "1216x832"},
    "album": "Картинки от Евы",
    "folder": "_EVA",
} | profile.ARTIST
PRINT = profile.service("print")
# file_id -> what was asked: a picture she made is known as hers (save, redraw)
MADE = {}


def _key():
    k = secrets().get(CFG["key"])
    if not k:
        raise RuntimeError(f"нет ключа API картинок ({CFG['key']} в secrets.env)")
    return k


def _images(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r).get("data") or []
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API картинок: HTTP {e.code} {e.read()[:300].decode('utf-8', 'replace')}")
    out = []
    for item in data:
        if item.get("b64_json"):
            out.append(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            with urllib.request.urlopen(item["url"], timeout=120) as r:
                out.append(r.read())
    if not out:
        raise RuntimeError("API картинок ничего не вернул (возможно, запрос отклонён фильтром)")
    return out[0]


def generate(prompt, size, model):
    body = json.dumps({"model": model, "prompt": prompt, "size": size, "n": 1}).encode()
    req = urllib.request.Request(CFG["base_url"] + "/images/generations", data=body, method="POST", headers={
        "Authorization": "Bearer " + _key(), "Content-Type": "application/json"})
    return _images(req, 300)


def edit(png, prompt, size):
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in {"model": CFG["edit_model"], "prompt": prompt, "size": size, "n": 1}.items()]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="in.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(CFG["base_url"] + "/images/edits", data=b"".join(parts), method="POST", headers={
        "Authorization": "Bearer " + _key(), "Content-Type": f"multipart/form-data; boundary={b}"})
    return _images(req, 600)


def _size(shape, src=None):
    """A size the API takes (multiples of 64); for a redraw "auto" keeps the
    source's proportions at about a megapixel (a 4:3 photo stays 4:3)."""
    sizes = CFG["sizes"]
    if shape in sizes:
        return sizes[shape]
    if src is not None:
        w, h = src
        k = (1024 * 1024 / (w * h)) ** 0.5
        w, h = (max(512, min(1536, round(x * k / 64) * 64)) for x in (w, h))
        return f"{w}x{h}"
    return sizes["square"]


def _jpeg(raw, size):
    from PIL import Image
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _keep(raw, name):
    """The picture into the print service's inbox. -> file_id"""
    req = urllib.request.Request(PRINT + "/files", data=raw, method="POST", headers={
        "Content-Type": "application/octet-stream", "X-Name": urllib.parse.quote(name), "X-Mime": "image/png"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["file_id"]


def _file(fid):
    with urllib.request.urlopen(f"{PRINT}/files/{fid}/raw", timeout=60) as r:
        return r.read()


def _result(raw, fid, title, note):
    """The picture for her eyes (small) and the presentation for the channel."""
    small = base64.b64encode(_jpeg(raw, 512)).decode("ascii")
    skills.HISTORY_IMAGES[small] = small
    return Result("", show={"kind": "picture", "file_id": fid, "caption": title}, blocks=[
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": small}},
        {"type": "text", "text": f"{note} Рисунок «{title}», file_id {fid}: показан. Напечатать - file_print, "
                                 f"сохранить в архив - picture_save, переделать - picture_redraw."}])


def draw(args, person):
    fast = args.get("quality") == "fast"
    model = CFG["fast_model"] if fast else CFG["model"]
    t0 = time.time()
    raw = generate(args["prompt"], _size(args.get("shape")), model)
    title = (args.get("title") or "рисунок").strip()
    fid = _keep(raw, f"{title}.png")
    MADE[fid] = {"title": title, "prompt": args["prompt"], "by": getattr(person, "name", None)}
    log(f"artist: drew {fid} in {time.time() - t0:.0f} s ({model})")
    return _result(raw, fid, title, f"Готово за {time.time() - t0:.0f} с.")


def redraw(args, person):
    if not CFG.get("edit_model"):
        return "Этот API картинок не умеет переделывать готовые - могу нарисовать заново по описанию."
    from PIL import Image
    if not args.get("photo_id") and not args.get("file_id"):
        # «сделай HDR» about the photo open on the TV: what the screen shows
        args = dict(args, **_on_screen(person))
        if args.get("error"):
            return args["error"]
    if args.get("photo_id"):
        src = _photo(int(args["photo_id"]))
    else:
        src = _file(args["file_id"])
    img = Image.open(io.BytesIO(src)).convert("RGB")
    size = _size(args.get("shape") or "auto", img.size)
    img.thumbnail((1536, 1536))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    t0 = time.time()
    raw = edit(buf.getvalue(), args["prompt"], size)
    title = (args.get("title") or "рисунок").strip()
    fid = _keep(raw, f"{title}.png")
    MADE[fid] = {"title": title, "prompt": args["prompt"], "by": getattr(person, "name", None),
                 "from": args.get("photo_id") or args.get("file_id")}
    log(f"artist: redrew {args.get('photo_id') or args.get('file_id')} -> {fid} in {time.time() - t0:.0f} s")
    return _result(raw, fid, title, f"Готово за {time.time() - t0:.0f} с.")


def _on_screen(person):
    """The picture on the TV now -> {"photo_id"} | {"file_id"} | {"error"}."""
    if person.channel != "voice":
        return {"error": "Какую картинку переделать: укажи photo_id (фото из архива) или file_id "
                         "(присланная картинка, твой рисунок)."}
    from core import screen
    s = screen.shown() or {}
    if s.get("photo"):
        return {"photo_id": s["photo"]}
    if s.get("file"):
        return {"file_id": s["file"]}
    if s.get("photos"):
        return {"error": f"На экране {len(s['photos'])} фото - какое переделать? Открой его (screen open) "
                         "или укажи photo_id."}
    return {"error": "На экране нет фото или рисунка - что переделать? Укажи photo_id или file_id."}


def _photo(image_id):
    with urllib.request.urlopen(f"{photolib.API}/image/{image_id}/full", timeout=60) as r:
        return r.read()


def _album(title):
    """The album by its title, made if there is none. -> album_id"""
    with urllib.request.urlopen(f"{photolib.API}/albums?search={urllib.parse.quote(title)}", timeout=15) as r:
        for a in json.load(r).get("albums", []):
            if a["title"].strip().lower() == title.strip().lower():
                return a["album_id"]
    req = urllib.request.Request(photolib.API + "/albums", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"title": title, "description": "Нарисовала Ева"},
                                                 ensure_ascii=False).encode())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["album_id"]


def save(args, person):
    fid = args["file_id"]
    raw = _file(fid)
    title = (MADE.get(fid) or {}).get("title") or args.get("title") or "рисунок"
    q = urllib.parse.urlencode({"folder": CFG["folder"], "name": title})
    req = urllib.request.Request(f"{photolib.API}/upload/indexed?{q}", data=raw, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=180) as r:      # indexed at once: CLIP on the hub
        image_id = json.load(r)["image_id"]
    album = (args.get("album") or CFG["album"]).strip()
    aid = _album(album)
    req = urllib.request.Request(f"{photolib.API}/albums/{aid}/photos", method="POST",
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps({"image_ids": [image_id]}).encode())
    urllib.request.urlopen(req, timeout=15).read()
    log(f"artist: saved {fid} as photo {image_id} into album {aid} «{album}»")
    return f"Сохранила в архив: фото {image_id}, альбом «{album}»."


_SHAPE = {"type": "string", "enum": ["square", "portrait", "landscape"],
          "description": "square - квадрат (по умолчанию), portrait - вертикальная (для печати на A4), "
                         "landscape - горизонтальная (для телевизора)"}
_TITLE = {"type": "string", "description": "короткое название по-русски («лиса с книжкой»)"}

PROMPT = """## Художник (рисунки)

Ты умеешь рисовать: picture_draw - картинка по описанию, picture_redraw -
обработать готовую картинку по указанию (image prompt): фото из архива по
photo_id, присланную картинку или свой рисунок по file_id. Просят про то, что
открыто на телевизоре («сделай HDR», «изобрази его лысым», «посади их на
яхту», «нарисуй такую же раскраску, но с собачкой») - picture_redraw без
photo_id и file_id: возьмётся то, что на экране.
- Prompt правки - по-английски, повелительно: что изменить и что сохранить.
  С людьми всегда: "keep the person's face, identity, pose and clothes
  unchanged". Примеры: HDR - "apply a strong HDR photo effect: bright
  shadows, detailed highlights, vivid colors, keep the composition and
  people unchanged"; лысый - "make the man bald, keep his face and identity
  unchanged, photorealistic"; яхта - "place the person on the deck of a
  sailing yacht at sea, sunny day, keep the face and identity unchanged,
  photorealistic"; в том же стиле, другой сюжет - "replace the panda with
  a puppy, keep exactly the same coloring-page style: black outlines on
  white, same line thickness, same layout".
- Людей не раздеваешь и не делаешь унизительного или пугающего; шутки
  (лысый, усы, костюм супергероя, другая эпоха) - пожалуйста.
- Описание (prompt) пиши по-английски, подробно: что изображено, стиль
  (детская книжка, акварель, аниме, мультфильм, раскраска - чёрные контуры
  на белом), цвета, фон. Надписей на картинке не проси: модели пишут буквы
  с ошибками, особенно по-русски.
- Рисунок идёт ~30 с (быстрый, quality=fast, ~10 с - когда просят быстро
  или черновик). Перед рисованием скажи коротко «сейчас нарисую».
- Готовый рисунок сам показывается (телевизор или фото в Телеграме), ты его
  видишь. Скажи одной фразой, что получилось, без описания каждой детали.
- По просьбе: напечатать - file_print с его file_id (принтер чёрно-белый:
  для печати детям раскраска выходит лучше цветной картинки); сохранить -
  picture_save (в архив, альбом «Картинки от Евы» или названный); не
  нравится - picture_redraw с поправкой или нарисуй заново.
- Детям рисуешь охотно; ничего страшного, жестокого и взрослого."""

SKILL = Skill(
    name="artist",
    summary="рисование картинок по описанию и переделка фото, показ, печать, сохранение в архив",
    prompt=PROMPT,
    tools=[Tool(
        name="picture_draw",
        description="Нарисовать картинку по описанию (генерация изображения). Картинка показывается сама; "
                    "возвращает её file_id.",
        schema={"type": "object", "additionalProperties": False, "required": ["prompt", "title"],
                "properties": {"prompt": {"type": "string", "description": "подробное описание по-английски"},
                               "title": _TITLE, "shape": _SHAPE,
                               "quality": {"type": "string", "enum": ["best", "fast"],
                                           "description": "best (~30 с, по умолчанию) | fast (~10 с)"}}},
        handler=draw, needs="known",
    ), Tool(
        name="picture_redraw",
        description="Обработать картинку по указанию (image prompt: эффект, другой фон, другая внешность, "
                    "тот же стиль с другим сюжетом): фото из архива (photo_id), присланную картинку или свой "
                    "рисунок (file_id), а без них - то, что открыто на телевизоре. Результат показывается сам.",
        schema={"type": "object", "additionalProperties": False, "required": ["prompt", "title"],
                "properties": {"prompt": {"type": "string", "description": "что сделать, по-английски "
                                          "(\"turn into a watercolor illustration, keep the faces\")"},
                               "photo_id": {"type": "integer", "description": "фото из архива; "
                                            "без photo_id и file_id - то, что сейчас на телевизоре"},
                               "file_id": {"type": "string"},
                               "title": _TITLE,
                               "shape": {"type": "string", "enum": ["auto", "square", "portrait", "landscape"],
                                         "description": "auto - как у исходной (по умолчанию)"}}},
        handler=redraw, needs="known",
    ), Tool(
        name="picture_save",
        description="Сохранить рисунок (file_id) в семейный фотоархив, в альбом «Картинки от Евы» "
                    "(или в названный album; нет такого - будет создан).",
        schema={"type": "object", "additionalProperties": False, "required": ["file_id"],
                "properties": {"file_id": {"type": "string"}, "title": _TITLE,
                               "album": {"type": "string"}}},
        handler=save, needs="known",
    )],
)
