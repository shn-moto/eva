#!/usr/bin/env python3
"""Eva in Telegram (@shn_eva_bot): her channel to the outside world.

Long-polls the Bot API (no third-party library). Who may write to her is
photolib's business: a Telegram account of a photolib user is family. Their
message goes to the brain (127.0.0.1:8081/chat), which decides who it is and
what they may use; her answer comes back as text. Anybody else gets a polite
refusal, and their id is logged (to be added in photolib if they are family).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import photolib  # noqa: E402
from core import profile  # noqa: E402
from core.util import log, secrets  # noqa: E402

OFFSET = "/var/lib/eva/telegram-offset"
BRAIN = "http://127.0.0.1:8081/chat"
TOKEN = secrets()['TELEGRAM_BOT_TOKEN']
API = f"https://api.telegram.org/bot{TOKEN}/"
FILES = profile.service("print")         # files sent to her are kept on the hub (services/print), not here


def call(method, http_timeout=60, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items()}).encode()
    with urllib.request.urlopen(API + method, data=data, timeout=http_timeout) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError(f"{method}: {res}")
    return res["result"]


def upload(method, files, **params):
    """multipart/form-data: photolib is at home, Telegram cannot fetch it by URL."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for k, v in params.items():
        v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for name, data in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{name}.jpg\"\r\n"
                 f"Content-Type: image/jpeg\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(API + method, data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError(f"{method}: {res}")
    return res["result"]


def image(path):
    with urllib.request.urlopen(photolib.API + path, timeout=30) as r:
        return r.read()


ALBUM = 10                       # Telegram's limit for one media group


def present(chat, p):
    """A tool's presentation as Telegram sees it (docs/ARCHITECTURE.md,
    "Presentations"): photos and a person's photos as an album, one photo as a photo."""
    kind = p.get("kind")
    if kind == "photo":
        upload("sendPhoto", {"p": image(f"/image/{p['id']}/full")}, chat_id=chat,
               photo="attach://p", caption=p.get("caption", "")[:1000])
        return
    if kind == "picture":
        # a picture she drew: a JPEG of it from the print service's inbox (skill artist)
        with urllib.request.urlopen(f"{profile.service('print')}/files/{p['file_id']}/jpeg?size=1568", timeout=60) as r:
            upload("sendPhoto", {"p": r.read()}, chat_id=chat, photo="attach://p", caption=p.get("caption", "")[:1000])
        return
    if kind == "person":
        items = p.get("items") or [{"id": i, "caption": ""} for i in p.get("photos", [])]
        title = p.get("name", "")
    elif kind == "photos":
        items, title = p.get("items", []), p.get("title", "")
    else:
        return
    items = items[:ALBUM]
    if not items:
        return
    files, media = {}, []
    for k, it in enumerate(items):
        try:
            files[f"p{k}"] = image(f"/image/{it['id']}/full")
        except OSError as e:
            log(f"image {it['id']}: {e}")
            continue
        m = {"type": "photo", "media": f"attach://p{k}"}
        # details of each photo, not only of the request: date, who is on it, GPS
        who = ", ".join(it.get("people") or [])
        line = " · ".join(x for x in (it.get("caption", ""), who) if x)
        cap = "\n".join(x for x in (title if not media else "", line,
                                    it.get("gps") and "GPS " + it["gps"]) if x)
        if cap:
            m["caption"] = cap[:1000]
        media.append(m)
    if len(media) == 1:
        upload("sendPhoto", files, chat_id=chat, photo=media[0]["media"], caption=media[0].get("caption", ""))
    elif media:
        upload("sendMediaGroup", files, chat_id=chat, media=media)


def brain(text, uid):
    req = urllib.request.Request(BRAIN, data=json.dumps(
        {"text": text, "telegram_id": uid, "key": f"tg:{uid}"}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:      # reading a PDF sent to her takes a while
        return json.loads(r.read())


def _attachment(msg):
    """A photo or a file in the message -> (telegram file_id, name, mime), or None."""
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"], "photo.jpg", "image/jpeg"     # the largest size
    d = msg.get("document")
    if d:
        return d["file_id"], d.get("file_name") or "file", d.get("mime_type") or ""
    return None


def keep_file(att):
    """Telegram -> the hub's inbox; -> the note she sees in the conversation."""
    tg_id, name, mime = att
    f = call("getFile", file_id=tg_id)
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{f['file_path']}", timeout=120) as r:
        data = r.read()
    req = urllib.request.Request(FILES + "/files", data=data, headers={
        "Content-Type": "application/octet-stream", "X-Name": urllib.parse.quote(name), "X-Mime": mime})
    with urllib.request.urlopen(req, timeout=60) as r:
        meta = json.loads(r.read())
    kind = {"pdf": "PDF", "image": "картинка", "office": "документ"}.get(meta["kind"], meta.get("mime") or "файл")
    pages = f", страниц {meta['pages']}" if meta["kind"] in ("pdf", "office") and meta.get("pages") else ""
    return f"[прислан файл: «{meta['name']}», {kind}{pages}, file_id={meta['file_id']}]"


def handle(msg, group=()):
    """msg: a message; group: the rest of its album (photos sent together come
    as separate messages - she answers once, with all of them)."""
    chat, user = msg["chat"]["id"], msg.get("from", {})
    uid = str(user.get("id"))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    for m in group:
        text = text or (m.get("caption") or "").strip()
    try:
        family = photolib.user_by_telegram(uid)
    except Exception as e:
        log("photolib:", e)
        family = None
    if not family:
        log(f"stranger: id {uid} @{user.get('username')} {user.get('first_name')!r}: {text[:60]!r}")
        call("sendMessage", chat_id=chat, text="Привет! Я Ева, домашний помощник одной семьи. "
             "С незнакомыми я, к сожалению, не переписываюсь.")
        return
    notes = []
    for m in (msg,) + tuple(group):
        att = _attachment(m)
        if att:
            try:
                notes.append(keep_file(att))
            except Exception as e:
                log(f"file from {uid}: {e}")
                call("sendMessage", chat_id=chat, text="Файл не смогла забрать (до 20 МБ), пришли ещё раз.")
                return
    if notes:
        text = "\n".join(x for x in [text] + notes if x)
    if not text:
        call("sendMessage", chat_id=chat, text="Пока я понимаю текст, фото и файлы.")
        return
    if text == "/start":
        text = "Привет! Это я, пишу тебе в Телеграме."
    log(f"{family.get('person_name')}: {text[:80]!r}")
    call("sendChatAction", chat_id=chat, action="typing")
    try:
        res = brain(text, uid)
        reply = res.get("reply") or ""
    except OSError as e:
        log("brain error:", e)
        res, reply = {}, "Ой, у меня что-то с головой, попробуй чуть позже."
    if reply:
        call("sendMessage", chat_id=chat, text=reply[:4000])
    for p in res.get("show") or []:
        try:
            call("sendChatAction", chat_id=chat, action="upload_photo")
            present(chat, p)
        except Exception as e:
            log(f"present {p.get('kind')}: {e}")


def main():
    try:
        offset = int(open(OFFSET).read())
    except (OSError, ValueError):
        offset = 0
    me = call("getMe")
    try:
        n = sum(1 for u in photolib.users() if u.get("telegram_id"))
    except Exception:
        n = "?"
    log(f"telegram up: @{me['username']}, family accounts from photolib: {n}")
    while True:
        try:
            ups = call("getUpdates", http_timeout=70, offset=offset, timeout=50)   # long poll 50 s
        except Exception as e:  # network away: try again a bit later
            log("poll error:", e)
            time.sleep(5)
            continue
        # an album (several photos at once) comes as messages with one
        # media_group_id: they go to her together, as one message
        albums = {}
        for u in ups:
            g = (u.get("message") or {}).get("media_group_id")
            if g:
                albums.setdefault(g, []).append(u["message"])
        for u in ups:
            offset = u["update_id"] + 1
            with open(OFFSET, "w") as f:
                f.write(str(offset))
            m = u.get("message")
            if not m:
                continue
            g = m.get("media_group_id")
            if g and albums.get(g) and albums[g][0] is not m:
                continue                      # handled with the first of its album
            try:
                handle(m, tuple(albums.get(g, [])[1:]) if g else ())
            except Exception as e:
                log("handle error:", e)


if __name__ == "__main__":
    main()
