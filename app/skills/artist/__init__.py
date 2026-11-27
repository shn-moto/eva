"""Skill "художник": she draws - a picture from words, or a photo / a sent
picture / her own earlier picture redrawn - shows it (the TV, or a photo in
Telegram), keeps it, prints it on request and saves it to the family archive.

Everything about drawing is here, the pictures API included. Which API is
profile.json "artist" (every key optional, the defaults are SAM Grid):

    "artist": {
      "base_url":   "https://api.grid.sam-solutions.net/v1",   an OpenAI-compatible images API:
                    POST /images/generations {model, prompt, size, n}, POST /images/edits (multipart)
      "key":        "GRID_KEY",               the name of its key in /etc/eva/secrets.env
      "draw_api":   "xai",                    who draws: "xai" (xAI images API) or "openai" (base_url)
      "model":      "grok-imagine-image-2.0", the best picture (~$0.04)
      "fast_model": "saipixel-krea2-turbo",    a quick one (~10 s)
      "edit_api":   "xai",                    who redraws a picture: "xai" (xAI images API, XAI_API_KEY)
                                                or "openai" (the base_url above, multipart /images/edits)
      "edit_model": "grok-imagine-image-2.0", the model of the redraw API (~20 s, ~$0.07)
      "fast_edit_model": "saipixel-flux2-klein-9b-edit",  a quick redraw (~15-25 s), weaker on big changes
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
    # drawing on xAI too (user, 2026-11-26): the SAM Grid models were far below it on the same photo
    "draw_api": "xai",
    "model": "grok-imagine-image-2.0",
    "fast_model": "saipixel-krea2-turbo",
    # drawing and editing apart (2026-11-26): on SAM Grid flux2-klein made two-headed wolves of «put her on
    # a wolf» and qwen-image-2.1 a girl grown into a small wolf; xAI's grok-imagine-image-2.0 turned the
    # statue under the girl into a living wolf she really sits on, her face and clothes kept (~20 s, $0.07)
    "edit_api": "xai",
    "edit_model": "grok-imagine-image-2.0",
    "fast_edit_model": "saipixel-flux2-klein-9b-edit",
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


ASPECT = {"square": "1:1", "portrait": "2:3", "landscape": "16:9"}   # xAI takes aspect ratios, not pixels


def generate(prompt, size, model, shape=None):
    """A picture from words. The fast model always goes to base_url (SAM Grid)."""
    if CFG.get("draw_api") == "xai" and model == CFG["model"]:
        k = secrets().get("XAI_API_KEY")
        if not k:
            raise RuntimeError("нет ключа xAI (XAI_API_KEY в secrets.env)")
        body = json.dumps({"model": model, "prompt": prompt, "n": 1, "response_format": "b64_json",
                           "aspect_ratio": ASPECT.get(shape or "square", "1:1")}).encode()
        req = urllib.request.Request("https://api.x.ai/v1/images/generations", data=body, method="POST",
                                     headers={"Authorization": "Bearer " + k, "Content-Type": "application/json"})
        return _images(req, 300)
    body = json.dumps({"model": model, "prompt": prompt, "size": size, "n": 1}).encode()
    req = urllib.request.Request(CFG["base_url"] + "/images/generations", data=body, method="POST", headers={
        "Authorization": "Bearer " + _key(), "Content-Type": "application/json"})
    return _images(req, 300)


def edit(png, prompt, size, model=None):
    """A picture redrawn by a prompt. The fast model always goes to base_url (SAM Grid)."""
    model = model or CFG["edit_model"]
    if CFG.get("edit_api") == "xai" and model == CFG["edit_model"]:
        return _edit_xai(png, prompt, model)
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in {"model": model, "prompt": prompt, "size": size, "n": 1}.items()]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="in.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(CFG["base_url"] + "/images/edits", data=b"".join(parts), method="POST", headers={
        "Authorization": "Bearer " + _key(), "Content-Type": f"multipart/form-data; boundary={b}"})
    return _images(req, 600)


def _edit_xai(png, prompt, model):
    """xAI images API: JSON, the picture as a data URI; the aspect follows the picture."""
    k = secrets().get("XAI_API_KEY")
    if not k:
        raise RuntimeError("нет ключа xAI (XAI_API_KEY в secrets.env)")
    body = {"model": model, "prompt": prompt, "response_format": "b64_json",
            "image": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"), "type": "image_url"}}
    req = urllib.request.Request("https://api.x.ai/v1/images/edits", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + k, "Content-Type": "application/json"})
    return _images(req, 300)


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


def _result(raw, fid, title, note, source=None):
    """The picture for her eyes (small) and the presentation for the channel. source: the
    picture it was made from (a redraw) - she sees both and can tell she took the wrong photo
    (the slideshow had moved on: «the girl on a wolf» came out with two men's heads, 2026-11-26)."""
    small = base64.b64encode(_jpeg(raw, 512)).decode("ascii")
    skills.HISTORY_IMAGES[small] = small
    before = []
    if source is not None:
        src = base64.b64encode(_jpeg(source, 384)).decode("ascii")
        skills.HISTORY_IMAGES[src] = src
        before = [{"type": "text", "text": "Исходная картинка (с неё переделано):"},
                  {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": src}},
                  {"type": "text", "text": "Результат:"}]
    return Result("", show={"kind": "picture", "file_id": fid, "caption": title}, blocks=before + [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": small}},
        {"type": "text", "text": f"{note} Рисунок «{title}», file_id {fid}: показан. Напечатать - file_print, "
                                 f"сохранить в архив - picture_save, переделать - picture_redraw."}])


def draw(args, person):
    fast = args.get("quality") == "fast"
    model = CFG["fast_model"] if fast else CFG["model"]
    t0 = time.time()
    raw = generate(args["prompt"], _size(args.get("shape")), model, args.get("shape"))
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
    model = CFG.get("fast_edit_model") if args.get("quality") == "fast" and CFG.get("fast_edit_model") else CFG["edit_model"]
    raw = edit(buf.getvalue(), args["prompt"], size, model)
    title = (args.get("title") or "рисунок").strip()
    fid = _keep(raw, f"{title}.png")
    MADE[fid] = {"title": title, "prompt": args["prompt"], "by": getattr(person, "name", None),
                 "from": args.get("photo_id") or args.get("file_id")}
    log(f"artist: redrew {args.get('photo_id') or args.get('file_id')} -> {fid} in {time.time() - t0:.0f} s ({model})")
    return _result(raw, fid, title, f"Готово за {time.time() - t0:.0f} с.", source=src)


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
          "description": "square (default), portrait (for printing on A4), landscape (for the TV)"}
_TITLE = {"type": "string", "description": "a short title in Russian («лиса с книжкой»)"}

PROMPT = """## Artist

A request about what is open on the TV («сделай HDR», «изобрази его лысым», «посади
их на яхту», «такую же раскраску, но с собачкой») - picture_redraw without photo_id
and file_id: it takes what is on the screen.
- Edit prompts in English, describing the RESULT, not operations: what must be in the
  picture ("Show the girl from this photo sitting on a big friendly grey wolf in a
  forest. Keep her face, hair and clothes. Only the girl and the wolf, nobody else."),
  not "remove the man, keep the pose" - that drew the dad's head in the girl's hands
  and two-headed wolves. With people always: "keep the person's face, identity, pose
  and clothes unchanged". Examples: HDR - "apply a strong HDR photo effect: bright
  shadows, detailed highlights, vivid colors, keep the composition and people
  unchanged"; bald - "make the man bald, keep his face and identity unchanged,
  photorealistic"; same style, new subject - "replace the panda with a puppy, keep
  exactly the same coloring-page style: black outlines on white, same line
  thickness, same layout". Several people - name whom you change ("the man on the
  left") and add "keep everyone else in the picture unchanged" (else the model drops
  them: the yacht kept the dad without his daughter).
- Draw prompts in English, detailed: subject, style (children's book, watercolor,
  anime, cartoon, coloring page - black outlines on white), colors, background. No
  text in the picture: models misspell letters, Russian ones most.
- Never undress people or make anything humiliating or scary; jokes (bald,
  moustache, superhero suit, another era) are fine. For children - gladly; nothing
  scary, cruel or adult.
- Drawing takes ~15-20 s (quality=fast ~10 s, simpler - when asked for quick or a
  draft). Say briefly «сейчас нарисую» first.
- Look at the result honestly before speaking. Extra heads or hands, strangers, the
  wrong scene or (after a redraw) the wrong source photo - say plainly it came out
  wrong and offer to redo. Never joke about a defect or pass it off as intended.
- The picture shows itself (TV or a photo in Telegram) and you see it: one sentence
  about what came out, not every detail.
- Print it - file_print with its file_id (the printer is black and white: a coloring
  page prints better for children than a colour picture); save - picture_save;
  disliked - picture_redraw with a correction, or draw anew."""

SKILL = Skill(
    name="artist",
    summary="рисование картинок по описанию и переделка фото, показ, печать, сохранение в архив",
    prompt=PROMPT,
    tools=[Tool(
        name="picture_draw",
        description=("Draw a picture from a description (image generation). It shows itself; returns its "
                     "file_id."),
        schema={"type": "object", "additionalProperties": False, "required": ["prompt", "title"],
                "properties": {"prompt": {"type": "string", "description": "a detailed description in English"},
                               "title": _TITLE, "shape": _SHAPE,
                               "quality": {"type": "string", "enum": ["best", "fast"],
                                           "description": "best (~15-20 s, default) | fast (~10 s, simpler)"}}},
        handler=draw, needs="known",
    ), Tool(
        name="picture_redraw",
        description=("Edit a picture by an instruction (image prompt: an effect, another background, another "
                     "look, the same style with another subject): an archive photo (photo_id), a sent picture "
                     "or your drawing (file_id), and without them what is open on the TV. The result shows "
                     "itself."),
        schema={"type": "object", "additionalProperties": False, "required": ["prompt", "title"],
                "properties": {"prompt": {"type": "string", "description": 'what to do, in English ("turn into a watercolor illustration, keep the faces")'},
                               "photo_id": {"type": "integer", "description": "an archive photo; without photo_id and file_id - what is on the TV now"},
                               "file_id": {"type": "string"},
                               "title": _TITLE,
                               "quality": {"type": "string", "enum": ["best", "fast"],
                                           "description": "best (~20 s, default) | fast (~20 s, weaker on big scene changes)"},
                               "shape": {"type": "string", "enum": ["auto", "square", "portrait", "landscape"],
                                         "description": "auto - as the source (default)"}}},
        handler=redraw, needs="known",
    ), Tool(
        name="picture_save",
        description=("Save a drawing (file_id) to the family photo archive, album «Картинки от Евы» (or the "
                     "named album; created if missing)."),
        schema={"type": "object", "additionalProperties": False, "required": ["file_id"],
                "properties": {"file_id": {"type": "string"}, "title": _TITLE,
                               "album": {"type": "string"}}},
        handler=save, needs="known",
    )],
)
