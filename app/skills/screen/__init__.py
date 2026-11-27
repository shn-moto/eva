"""Skill "экран": navigation over whatever a tool put on the TV (core/screen.py),
the same words for every presentation: «дальше», «открой третью», «закрой»."""
from core import screen
from core.skills import Skill, Tool
from core.speech import ui

AVATARS = {"ascii": "лицо из символов (псевдографика)", "image": "лицо картинкой (аниме)",
           "eq": "без лица: полоски-эквалайзер, которые двигаются, когда ты говоришь",
           "live2d": "живая модель Live2D (пока пробная: девушка Hiyori), двигается, моргает, говорит"}


def show(args, person):
    """A page she wrote herself - a chart, a table, a comparison - on the TV."""
    from core.skills import Result
    html = args["html"]
    if len(html) > 200_000:
        return "Страница слишком большая (больше 200 КБ) - упрости."
    full = bool(args.get("fullscreen"))
    return Result(f"Показала на телевизоре «{args['title']}»{' на весь экран' if full else ''}.",
                  {"kind": "html", "title": args["title"], "html": html, "full": full})


SHOW_HTML = """Show your own page on the TV: a chart, a table, a comparison - whatever you design
for the data. html - a whole document (<html>…) drawn in a frame: in the window
(your face column on the right) about 880x560 px, fullscreen 1280x720. Layout for
that frame: 100vw/100vh, no scrolling, large font (20 px+, a TV seen from the sofa),
dark background (#05070d), light text, accent #8fe9ff. Charts: <script
src="/lib/chart.umd.min.js"></script> (Chart.js 4, offline), animation: false (weak
GPU). Put the data in the page. No external links or images from the internet."""


def spend(args, person):
    """What Claude cost: today, by model and channel, and the last days."""
    from core import cost
    t = cost.today()
    return (f"Сегодня ${t['usd']:.2f} из лимита ${cost.DAILY_LIMIT:.2f}, вызовов {t['calls']}; по каналам: "
            + ", ".join(f"{k} ${v:.2f}" for k, v in t.get("by_channel", {}).items())
            + ". По дням:\n" + cost.summary(7))


def set_avatar(args, person):
    """How she looks on the TV; ui_server remembers it (/var/lib/eva/avatar)."""
    mode = args["mode"]
    ui({"type": "avatar", "mode": mode})
    return f"Теперь на экране: {AVATARS[mode]}."


def panel(args, person):
    """The chat column and the debug status bar on the TV; ui_server keeps them."""
    on = bool(args["show"])
    ui({"type": "panel", "panel": args["panel"], "on": on})
    if args["panel"] == "stats" and on:
        from core import cost
        ui(cost.stats())
    name = {"chat": "окно чата", "stats": "панель расходов"}[args["panel"]]
    return f"{name.capitalize()} {'показано' if on else 'скрыто'}."


def act(args, person):
    n = args.get("n")
    return screen.act(args["action"], int(n) if isinstance(n, (int, float)) else None)


PROMPT = """## Screen (TV)

What a tool shows appears on the TV, photos numbered. «Дальше», «назад», «открой
третью», «закрой», «вернись к лицу» - the screen tool. When showing, speak briefly
(«вот, смотри»), do not list what is visible anyway. Data (energy, finances,
anything) can be shown as your own page (show_html) - choose the form yourself
(bars, lines, table, big numbers); fullscreen when asked. Anything shown can be
printed: «напечатай это» - print_screen; for paper a separate light page is better -
print_page. live2d is still a trial (Hiyori). If your face makes someone
uncomfortable, offer the equalizer."""

SKILL = Skill(
    name="screen",
    summary="навигация по экрану телевизора",
    prompt=PROMPT,
    tools=[Tool(
        name="screen",
        description=("Control what the TV shows: next - next page or photo, prev - back, open - photo number n "
                     "full screen, back - from a photo back to the grid, close - clear everything, back to the "
                     "face."),
        schema={"type": "object", "additionalProperties": False, "required": ["action"],
                "properties": {"action": {"type": "string", "enum": ["next", "prev", "open", "back", "close"]},
                               "n": {"type": "integer", "description": "photo number for open"}}},
        handler=act,
        needs="screen",
    ), Tool(
        name="show_html",
        description=SHOW_HTML,
        schema={"type": "object", "additionalProperties": False, "required": ["title", "html"],
                "properties": {"title": {"type": "string", "description": "a short title in Russian"},
                               "html": {"type": "string"},
                               "fullscreen": {"type": "boolean", "description": "fullscreen (when asked)"}}},
        handler=show,
        needs="screen",
    ), Tool(
        name="eva_spend",
        description=("What your answers cost (Claude API): today, by model and channel, by day; the daily "
                     "limit."),
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=spend,
        needs="admin",
    ), Tool(
        name="panel",
        description=("Show or hide a panel on the TV: chat - the chat window on the right (the conversation's "
                     "lines), stats - a thin debug bar on top (what your answers cost today and this month, "
                     "tokens and cache by model). «Убери чат», «покажи расходы», «скрой панель»."),
        schema={"type": "object", "additionalProperties": False, "required": ["panel", "show"],
                "properties": {"panel": {"type": "string", "enum": ["chat", "stats"]},
                               "show": {"type": "boolean"}}},
        handler=panel,
        needs="known",
    ), Tool(
        name="avatar",
        description=("Change how you look on the TV: ascii - a face of characters, image - a picture face "
                     "(anime), eq - no face, a voice equalizer, live2d - a live Live2D model («стань "
                     "эквалайзером», «верни лицо», «включи живую модель»)."),
        schema={"type": "object", "additionalProperties": False, "required": ["mode"],
                "properties": {"mode": {"type": "string", "enum": ["ascii", "image", "eq", "live2d"]}}},
        handler=set_avatar,
        needs=None,
    )],
)
