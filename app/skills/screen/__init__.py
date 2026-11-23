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


SHOW_HTML = """Показать на телевизоре свою страницу: график, таблицу, сравнение - всё, что
сама придумаешь для данных. html - документ целиком (<html>…), он рисуется в рамке:
в окне (справа колонка с твоим лицом) это примерно 880x560 px, на весь экран (fullscreen) - 1280x720.
Вёрстка под эту рамку: 100vw/100vh, без прокрутки, крупный шрифт (от 20 px, это
телевизор с дивана), тёмный фон (#05070d), светлый текст, акцент #8fe9ff.
Графики: <script src="/lib/chart.umd.min.js"></script> (Chart.js 4, без интернета),
animation: false (слабая видеокарта). Данные вставь прямо в страницу. Без внешних
ссылок и картинок из интернета."""


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


PROMPT = """## Экран (телевизор)

Когда инструмент что-то показывает, это появляется на телевизоре: фото
пронумерованы. «Дальше», «назад», «открой третью», «закрой», «вернись к лицу»
- это инструмент screen. Показывая, говори коротко («вот, смотри»), не
перечисляй то, что и так видно на экране.
Данные (энергия, финансы, что угодно) можно показать своей страницей - show_html:
сама реши, как их лучше показать (столбцы, линии, таблица, крупные цифры).
По просьбе «на весь экран» - fullscreen.
Как ты выглядишь на экране, можно менять (avatar): ascii - лицо из символов,
image - лицо картинкой, eq - без лица, эквалайзер голоса, live2d - живая модель
(пока пробная, Hiyori). Если кому-то неуютно
от твоего лица - предложи эквалайзер.
Окно чата справа и отладочную панель расходов сверху показываешь и прячешь
инструментом panel."""

SKILL = Skill(
    name="screen",
    summary="навигация по экрану телевизора",
    prompt=PROMPT,
    tools=[Tool(
        name="screen",
        description="Управление тем, что показано на телевизоре: next - дальше (следующая страница или "
                    "следующее фото), prev - назад, open - открыть фото номер n на весь экран, back - "
                    "вернуться из фото к сетке, close - убрать всё и вернуться к лицу.",
        schema={"type": "object", "additionalProperties": False, "required": ["action"],
                "properties": {"action": {"type": "string", "enum": ["next", "prev", "open", "back", "close"]},
                               "n": {"type": "integer", "description": "номер фото для open"}}},
        handler=act,
        needs="screen",
    ), Tool(
        name="show_html",
        description=SHOW_HTML,
        schema={"type": "object", "additionalProperties": False, "required": ["title", "html"],
                "properties": {"title": {"type": "string", "description": "короткий заголовок по-русски"},
                               "html": {"type": "string"},
                               "fullscreen": {"type": "boolean", "description": "на весь экран (по просьбе)"}}},
        handler=show,
        needs="screen",
    ), Tool(
        name="eva_spend",
        description="Сколько стоили твои ответы (Claude API): сегодня, по моделям и каналам, по дням; дневной лимит.",
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=spend,
        needs="admin",
    ), Tool(
        name="panel",
        description="Показать или скрыть панель на телевизоре: chat - окно чата справа (реплики разговора), "
                    "stats - тонкая отладочная панель сверху (сколько стоят твои ответы сегодня и за месяц, "
                    "токены и кэш по моделям). «Убери чат», «покажи расходы», «скрой панель».",
        schema={"type": "object", "additionalProperties": False, "required": ["panel", "show"],
                "properties": {"panel": {"type": "string", "enum": ["chat", "stats"]},
                               "show": {"type": "boolean"}}},
        handler=panel,
        needs="known",
    ), Tool(
        name="avatar",
        description="Сменить, как ты выглядишь на телевизоре: ascii - лицо из символов, image - лицо "
                    "картинкой (аниме), eq - без лица, эквалайзер голоса, live2d - живая модель Live2D («стань "
                    "эквалайзером», «верни лицо», «включи живую модель»).",
        schema={"type": "object", "additionalProperties": False, "required": ["mode"],
                "properties": {"mode": {"type": "string", "enum": ["ascii", "image", "eq", "live2d"]}}},
        handler=set_avatar,
        needs=None,
    )],
)
