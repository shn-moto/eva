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
from core.util import log, secrets  # noqa: E402

OFFSET = "/var/lib/eva/telegram-offset"
BRAIN = "http://127.0.0.1:8081/chat"
API = f"https://api.telegram.org/bot{secrets()['TELEGRAM_BOT_TOKEN']}/"


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
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def handle(msg):
    chat, user = msg["chat"]["id"], msg.get("from", {})
    uid, text = str(user.get("id")), (msg.get("text") or "").strip()
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
    if not text:
        call("sendMessage", chat_id=chat, text="Пока я понимаю только текст.")
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
        for u in ups:
            offset = u["update_id"] + 1
            with open(OFFSET, "w") as f:
                f.write(str(offset))
            if "message" in u:
                try:
                    handle(u["message"])
                except Exception as e:
                    log("handle error:", e)


if __name__ == "__main__":
    main()
