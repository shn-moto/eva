"""The media library ("медиатека"): the hub's own folders of video and music (services/music/video.py), chosen on the
admin page «Ресурсы». Folders and files as tiles on the TV (posters cut from the films, covers of the
music); a film goes to the DLNA renderer chosen there (a TV's own player - the Station does not decode
it), music plays through the Station like any track. A whole folder plays as a series / an album.
Driven only by the local router (dialog.local: library_open / library_find / screen_open / screen_back /
video_control) - never through the paid API: Sonnet opened it for $0.19 and the user ruled that local
resources must not cost a cent (2026-11-27). Claude has no tool for it."""
import json
import urllib.parse

import music
from core import screen
from core.skills import Result

KINDS = ("video", "music")


def _get(path, timeout=30):
    return music._get(path, timeout)


def _target(args):
    """The item the request is about: by its number on the TV, else by path."""
    n = args.get("n")
    if n:
        p = screen.state.get("p") or {}
        items = (p.get("items") or []) if p.get("kind") == "library" else []
        if not 1 <= n <= len(items):
            raise ValueError(f"на экране нет номера {n}")
        it = items[n - 1]
        return it["path"], it["folder"], p.get("lib", "video")
    return args.get("path") or "", None, args.get("kind") or "video"


def _show(r, kind, title):
    items = r.get("items") or []
    tiles = [{"name": it["name"], "path": it["path"], "folder": it["folder"], "count": it.get("count"),
              "src": music.SERVICE + it["poster"]} for it in items]
    pres = {"kind": "library", "lib": kind, "title": title, "dir": r.get("dir"), "parent": r.get("parent"),
            "items": tiles}
    lines = [f"{k}. {it['name']}" + (f" (папка, {it.get('count')})" if it["folder"] else "")
             for k, it in enumerate(items[:60], 1)]
    head = f"Показала на экране {title}: {len(items)}" + (" (пронумерованы)." if items else ", пусто.")
    return Result(head + "\n" + "\n".join(lines) + ("\n…" if len(items) > 60 else ""), pres)


def browse(args, person):
    try:
        if args.get("query"):
            kind = args.get("kind") or "video"
            r = _get(f"/library/find?kind={kind}&q={urllib.parse.quote(args['query'])}")
            return _show(r, kind, f"найдено «{args['query']}»")
        if args.get("up"):
            p = screen.state.get("p") or {}
            path, kind = (p.get("parent") or ""), p.get("lib", args.get("kind") or "video")
        else:
            path, folder, kind = _target(args)
            if folder is False:
                return "Это файл, не папка: его можно включить (library_play)."
        r = _get(f"/library/list?kind={kind}&dir={urllib.parse.quote(path)}")
    except (OSError, ValueError) as e:
        return f"Медиатека недоступна: {e}"
    if r.get("error"):
        return r["error"]
    name = path.rstrip("/").rsplit("/", 1)[-1] if path else ("видеотека" if kind == "video" else "музыка")
    if not path and not r.get("items"):
        return "В медиатеке не задано ни одной папки (админка, «Ресурсы»)."
    return _show(r, kind, name)


def play(args, person):
    try:
        path, folder, kind = _target(args)
        if not path:
            return "Что включить? Назови номер на экране или путь."
        if kind == "video":
            r = _get(f"/dlna/play?path={urllib.parse.quote(path)}&all={'1' if args.get('all') else '0'}", 60)
            if r.get("error"):
                return f"Не получилось: {r['error']}"
            screen.act("close")        # the TV shows the film now, not her tiles
            series = f", серия {r['index']} из {r['count']}, следующие пойдут сами" if r.get("count", 1) > 1 else ""
            return f"Включила на телевизоре: {r.get('title')}{series}. Если ответить больше нечего - ответь <молчу>."
        if folder or args.get("all"):
            d = path if folder else path.rsplit("/", 1)[0]
            files = _get(f"/library/files?kind=music&dir={urllib.parse.quote(d)}")
            tracks = [{"title": f.rsplit("/", 1)[-1].rsplit(".", 1)[0], "url": f} for f in files]
            if not folder:
                i = files.index(path) if path in files else 0
                tracks = tracks[i:] + tracks[:i]
        else:
            tracks = [{"title": path.rsplit("/", 1)[-1].rsplit(".", 1)[0], "url": path}]
        if not tracks:
            return "В папке нет музыки."
        music.PLAYER.play(tracks)
        screen.act("close")            # the player comes up instead of the tiles
        return (f"Включила: {tracks[0]['title']}" + (f", в очереди ещё {len(tracks) - 1}" if len(tracks) > 1 else "")
                + ". Вслух про музыку ничего не говори; если ответить больше нечего - ответь <молчу>.")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return f"Не получилось: {e}"


def control(args, person):
    a = args["action"]
    try:
        if a == "status":
            r = _get("/dlna/state")
        else:
            q = f"&to={urllib.parse.quote(args['to'])}" if args.get("to") else ""
            r = _get(f"/dlna/control?action={a}{q}")
    except OSError as e:
        return f"Не получилось: {e}"
    if r.get("error"):
        return r["error"]
    if not r.get("count"):
        return "Видео не играет." if a == "status" else "Готово."
    m, s = divmod(r.get("pos") or 0, 60)
    return (f"{r.get('title')}: {'пауза' if r.get('state') == 'paused' else r.get('state')}, {m // 60}:{m % 60:02d}:{s:02d}"
            + (f" из {r['dur'] // 3600}:{r['dur'] % 3600 // 60:02d}:{r['dur'] % 60:02d}" if r.get("dur") else "")
            + (f", серия {r['index']} из {r['count']}" if r["count"] > 1 else ""))
