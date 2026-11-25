"""The TV beyond the face: presentations from tools (docs/ARCHITECTURE.md,
"Presentations") and navigation over them.

A tool returns a presentation (data, not UI):
    {"kind": "photos", "title", "items": [{"id", "caption"}]}
    {"kind": "photo",  "id", "caption"}
    {"kind": "person", "name", "face", "lines": [...], "photos": [ids]}
    {"kind": "picture", "file_id", "caption"}   a picture she drew (skill artist)
The brain keeps what is on screen (so «дальше», «открой третью» work for any
presentation) and sends the page a ready-to-draw "view" event; the page only
draws it, with the face in a corner. Images come through ui_server (/img, /face).
"""
import threading
import time

from core.speech import ui
from core.util import log
from core import profile

PER_PAGE = profile.tune("screen.per_page", 11)                # 4 x 3 grid on the TV, the 12th place is the face in the corner
IDLE_CLOSE = profile.tune("screen.idle_close", 180)             # s without a word about the screen: back to the face

_lock = threading.Lock()
state = {"p": None, "page": 0, "open": None, "touched": 0.0}


def present(p):
    """A tool's presentation goes on the TV (voice channel)."""
    if not p or p.get("kind") not in ("photos", "photo", "person", "html", "picture"):
        return
    with _lock:
        state.update(p=p, page=0, open=None, touched=time.time())
    log(f"screen: {p['kind']} {p.get('title') or p.get('name') or p.get('id')}")
    _send()


def _items():
    p = state["p"]
    if not p:
        return []
    if p["kind"] == "photos":
        return p["items"]
    if p["kind"] in ("html", "picture"):
        return []
    if p["kind"] == "person":
        return [{"id": i, "caption": ""} for i in p.get("photos", [])]
    return [{"id": p["id"], "caption": p.get("caption", "")}]


def _back():
    """Nothing shown on top: the slideshow if it runs, the music player if
    music plays, else the face."""
    import music
    import slideshow
    if slideshow.reshow():
        return
    ev = music.PLAYER.view_event()
    ui(ev or {"type": "view", "kind": "close"})


def back():
    """For the slideshow when it stops: the player or the face."""
    _back()


def music_closed():
    """The music stopped: the face comes back (unless pictures or the
    slideshow are shown - the slideshow just loses its track line)."""
    import slideshow
    if state["p"] is None and not slideshow.reshow():
        ui({"type": "view", "kind": "close"})


def _send():
    p, items = state["p"], _items()
    if p is None:
        _back()
        return
    if state["open"] is not None and items:
        i = state["open"]
        it = items[i]
        ui({"type": "view", "kind": "photo", "id": it["id"], "caption": it.get("caption", ""),
            "index": i + 1, "count": len(items)})
        return
    if p["kind"] == "photo":
        ui({"type": "view", "kind": "photo", "id": p["id"], "caption": p.get("caption", ""), "index": 1, "count": 1})
        return
    if p["kind"] == "picture":
        ui({"type": "view", "kind": "photo", "file": p["file_id"], "caption": p.get("caption", ""), "index": 1, "count": 1})
        return
    if p["kind"] == "html":
        # a page she wrote (a chart, a table): in the view with her face in the
        # corner, or the whole screen
        ui({"type": "view", "kind": "html", "title": p.get("title", ""), "html": p["html"], "full": bool(p.get("full"))})
        return
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = min(state["page"], pages - 1)
    first = page * PER_PAGE
    grid = [dict(it, n=first + k + 1) for k, it in enumerate(items[first:first + PER_PAGE])]
    if p["kind"] == "person":
        ui({"type": "view", "kind": "person", "name": p["name"], "face": p.get("face"),
            "lines": p.get("lines", []), "items": grid, "page": page + 1, "pages": pages})
    else:
        ui({"type": "view", "kind": "photos", "title": p.get("title", ""), "items": grid,
            "page": page + 1, "pages": pages, "total": len(items)})


def item(n=None):
    """The image id of photo n on the screen, or of the one open now. -> id | None"""
    items = _items()
    if n is None:
        if state["open"] is not None and items:
            return items[state["open"]]["id"]
        p = state["p"]
        return p["id"] if p and p["kind"] == "photo" else None
    return items[n - 1]["id"] if 1 <= n <= len(items) else None


def shown():
    """What is on the TV now, for printing it: {"photo": id} | {"file": file_id,
    "title"} | {"html", "title"} | {"photos": [ids], "title"} | None (the face, the player)."""
    p = state["p"]
    if p is None:
        import slideshow
        c = slideshow.state.get("current") if slideshow.state.get("on") else slideshow.held_photo()
        if not c and slideshow.state.get("last") and time.time() - slideshow.state["last"][1] < 60:
            c = slideshow.state["last"][0]   # it stops as soon as she is called
        return {"photo": c["id"], "title": c.get("title", "")} if c else None
    if state["open"] is not None or p["kind"] == "photo":
        return {"photo": item(), "title": p.get("caption") or p.get("title", "")}
    if p["kind"] == "picture":
        return {"file": p["file_id"], "title": p.get("caption", "")}
    if p["kind"] == "html":
        return {"html": p["html"], "title": p.get("title", "")}
    pages = max(1, (len(_items()) + PER_PAGE - 1) // PER_PAGE)
    first = min(state["page"], pages - 1) * PER_PAGE
    return {"photos": [it["id"] for it in _items()[first:first + PER_PAGE]],
            "title": p.get("title") or p.get("name", "")}


def describe():
    """What is on the screen now, in words, for the model."""
    p = state["p"]
    if p is None:
        import music
        import slideshow
        shown = slideshow.describe()
        if music.PLAYER.active():
            # the slideshow runs over music too: both, the photo first
            return (shown + " " if shown else "На экране плеер. ") + music.PLAYER.describe()
        return shown or "На экране только твоё лицо."
    items = _items()
    if state["open"] is not None:
        return f"Открыто фото {state['open'] + 1} из {len(items)} (photo_id {items[state['open']]['id']})."
    if p["kind"] == "photo":
        return f"На экране одно фото (photo_id {p['id']})."
    if p["kind"] == "picture":
        return f"На экране твой рисунок «{p.get('caption', '')}» (file_id {p['file_id']})."
    if p["kind"] == "html":
        return f"На экране твоя страница «{p.get('title', '')}»{' на весь экран' if p.get('full') else ' (в окне)'}."
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    return (f"На экране {'карточка ' + p['name'] if p['kind'] == 'person' else 'фото: ' + p.get('title', '')}, "
            f"страница {state['page'] + 1} из {pages}, всего фото {len(items)} (пронумерованы).")


def act(action, n=None):
    """Navigation: next | prev | open N | close | back. -> text for the model."""
    with _lock:
        if state["p"] is None and action != "close":
            _back()                   # the player again, if music plays (else the face)
            return describe()
        state["touched"] = time.time()
        items = _items()
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        if action == "close":
            state.update(p=None, open=None, page=0)
        elif action == "back":
            if state["open"] is not None:
                state["open"] = None
            else:
                state.update(p=None, page=0)
        elif action == "open":
            if not n or not 1 <= n <= len(items):
                return f"Нет фото с номером {n}: их {len(items)}."
            state["open"] = n - 1
            state["page"] = (n - 1) // PER_PAGE
        elif action in ("next", "prev"):
            step = 1 if action == "next" else -1
            if state["open"] is not None:
                state["open"] = (state["open"] + step) % len(items)
            else:
                state["page"] = max(0, min(pages - 1, state["page"] + step))
        else:
            return f"Не знаю действия {action}."
        _send()
        return describe()


def watch():
    """Back to the face after a while without a word about the screen."""
    while True:
        time.sleep(5)
        with _lock:
            if state["p"] is not None and time.time() - state["touched"] > IDLE_CLOSE:
                state.update(p=None, open=None, page=0)
                log("screen: closed (idle)")
                _send()
