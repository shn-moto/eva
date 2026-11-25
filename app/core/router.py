"""The first stage for every phrase: a local model (llama.cpp on the GPU of
shn-linux) decides whether it is meant for Eva at all and, if it is, whether
it is a simple command from the catalog below - those are done locally and
never reach Claude. Only "claude" goes on to the paid model.

Why: a three-word music command cost $0.16 (26k tokens of persona, 25 tool
specs and history, cache cold) and the morning's «включи музыку» + «стоп»
24 cents (2026-11-23). The router answers in ~0.2 s on the GPU.

The output is JSON held to SCHEMA by llama.cpp's grammar, so a small model
cannot answer in prose. On any failure route() returns None and the caller
falls back to the old way (gate + Claude).
"""
import json
import os
import time
import urllib.request

from core import profile
from core.util import log

URL = profile.service("router")
TIMEOUT = profile.tune("router.timeout", 3.0)

# intent -> what it needs in args. "claude": anything that needs thinking,
# facts, memory, photos, money, people, conversation.
INTENTS = {
    "ignore": "фраза не Еве: люди говорят между собой, телевизор, обрывок без обращения",
    "unclear": "обращаются к Еве, но фраза бессмысленная или обрезанная (ошибка распознавания)",
    "ack": "ТОЛЬКО реакция без какой-либо просьбы: хорошо, отлично, спасибо, понятно, ладно, ок (если после 'хорошо' идёт просьба - это та просьба)",
    "claude": "всё остальное: вопросы, разговор, сказки, фото, поиск, память, люди, деньги, дом, сообщения",
    "music_play": "включить музыку: args.query - что искать (исполнитель/песня/жанр, понятное название), args.kind - track (одна песня), selection (подборка, жанр, исполнитель без песни), album. Если непонятно, что включить, или нужен контекст ('такую же', 'как сейчас играла') - claude",
    "music_stop": "выключить музыку совсем",
    "music_pause": "пауза",
    "music_resume": "продолжить после паузы",
    "music_next": "следующий трек",
    "music_prev": "предыдущий трек",
    "what_playing": "что сейчас играет",
    "volume_up": "громче",
    "volume_down": "тише",
    "volume_set": "громкость на N процентов: args.level 0-100",
    "screen_next": "на экране дальше / следующая страница / пролистай",
    "screen_prev": "на экране назад / предыдущая страница",
    "screen_open": "открыть фото номер N на экране: args.n",
    "screen_back": "из открытого фото назад к сетке",
    "screen_close": "убрать всё с экрана, вернуться к лицу / на главный экран",
    "avatar": "сменить её вид на экране: args.mode - ascii (лицо из символов), image (аниме лицо, картинка), eq (эквалайзер, без лица), live2d (живая модель)",
    "panel": "показать/скрыть панель на экране: args.panel - chat (окно чата) или stats (панель статуса: её собственные траты на API, токены), args.show true/false. Семейные расходы, деньги, траты по месяцам - это claude",
    "time": "который час / какое сегодня число / день недели",
    "print_screen": "распечатать то, что сейчас на экране: «распечатай это», «напечатай эту картинку / это фото / раскраску», «распечатай» без уточнения. Если сказано, что печатать, и это не «это» (список покупок, рецепт, фото с моря, отчёт) - claude",
}

SCHEMA = {
    "type": "object",
    "properties": {
        # first, so the model decides who is spoken to before what is asked
        "to_eva": {"type": "boolean"},
        "intent": {"type": "string", "enum": list(INTENTS)},
        "query": {"type": "string"},
        "kind": {"type": "string", "enum": ["track", "selection", "album"]},
        "level": {"type": "integer"},
        "n": {"type": "integer"},
        "mode": {"type": "string", "enum": ["ascii", "image", "eq", "live2d"]},
        "panel": {"type": "string", "enum": ["chat", "stats"]},
        "show": {"type": "boolean"},
        # said to the end? false: the person goes on («Ева, напиши моей маме» ...
        # pause ... «что я её жду») - the dialog waits for the rest
        "complete": {"type": "boolean"},
    },
    "required": ["to_eva", "intent"],
}

SYSTEM = ("Ты классификатор фраз для домашнего голосового помощника Евы. Фраза распознана из речи "
          "и может содержать ошибки. Выбери ровно один intent и заполни нужные поля.\n\nIntents:\n"
          + "\n".join(f"- {k}: {v}" for k, v in INTENTS.items()) + """

Правила:
- Сначала to_eva: обращена ли фраза к Еве. Если обращения по имени нет и разговора с Евой нет - to_eva
  почти всегда false: в доме люди говорят друг с другом, с детьми, по телефону, работает телевизор.
  Исключение: явная команда помощнику (включи/выключи музыку, громче, тише, следующую песню).
- Если к Еве обратились по имени или идёт разговор с ней - to_eva true.
- to_eva false -> intent ignore.
- Если сомневаешься между простой командой и claude - claude.
- Вопрос, просьба рассказать, найти, посмотреть, передать, запомнить, показать фото или данные - claude.
- "ева ты слушаешь", "ева ты тут" - claude.
- music_* и what_playing - только про музыку; "стоп"/"выключи" при играющей музыке - music_stop.
- complete: false, если просьба явно не договорена и человек сейчас продолжит: не сказано,
  что написать или передать, что включить, или фраза оборвана на полуслове. Законченная
  просьба или вопрос - true.

Примеры (контекст: обращение по имени или разговор, если не сказано иначе):
"ева включи квин" -> {"to_eva":true,"intent":"music_play","query":"Queen","kind":"selection"}
"включи Bohemian Rhapsody" -> {"to_eva":true,"intent":"music_play","query":"Bohemian Rhapsody","kind":"track"}
"включи детские песни" -> {"to_eva":true,"intent":"music_play","query":"детские песни","kind":"selection"}
"хорошо включи ка мне наутилус" -> {"to_eva":true,"intent":"music_play","query":"Наутилус Помпилиус","kind":"selection"}
"ева включи" -> {"to_eva":true,"intent":"claude"}
"включи такую же как сейчас играла" -> {"to_eva":true,"intent":"claude"}
"сделай погромче" -> {"to_eva":true,"intent":"volume_up"}
"громкость на тридцать" -> {"to_eva":true,"intent":"volume_set","level":30}
"открой седьмое" -> {"to_eva":true,"intent":"screen_open","n":7}
"следующую страницу" -> {"to_eva":true,"intent":"screen_next"}
"хорошо можешь вернуться на главный экран" -> {"to_eva":true,"intent":"screen_close"}
"спасибо верни главный экран" -> {"to_eva":true,"intent":"screen_close"}
"покажи аниме лицо" -> {"to_eva":true,"intent":"avatar","mode":"image"}
"убери чат" -> {"to_eva":true,"intent":"panel","panel":"chat","show":false}
"покажи панель статуса" -> {"to_eva":true,"intent":"panel","panel":"stats","show":true}
"покажи расходы на продукты за октябрь" -> {"to_eva":true,"intent":"claude"}
"спасибо" -> {"to_eva":true,"intent":"ack"}
"хорошо я понял" -> {"to_eva":true,"intent":"ack"}
"ева покажи фото из устки" -> {"to_eva":true,"intent":"claude"}
"кто на третьем фото" -> {"to_eva":true,"intent":"claude"}
"ева ты слушаешь" -> {"to_eva":true,"intent":"claude"}
"ева напиши моей маме" -> {"to_eva":true,"intent":"claude","complete":false}
"ева передай папе что" -> {"to_eva":true,"intent":"claude","complete":false}
"ева напиши маме что я скоро буду" -> {"to_eva":true,"intent":"claude","complete":true}
"ева какая завтра погода" -> {"to_eva":true,"intent":"claude","complete":true}
"ева распечатай это" -> {"to_eva":true,"intent":"print_screen"}
"распечатай пожалуйста эту раскраску" -> {"to_eva":true,"intent":"print_screen"}
"ева распечатай список покупок" -> {"to_eva":true,"intent":"claude"}
"ева станет там" -> {"to_eva":true,"intent":"unclear"}
Без имени и вне разговора:
"мам почему у меня такая книга" -> {"to_eva":false,"intent":"ignore"}
"ты должна немножко понимать" -> {"to_eva":false,"intent":"ignore"}
"а ты что будешь" -> {"to_eva":false,"intent":"ignore"}
"кто такая ольга" -> {"to_eva":false,"intent":"ignore"}
"выключи музыку" -> {"to_eva":true,"intent":"music_stop"}""")


def route(text, named=False, in_dialog=False, music_on=False, screen=None, last_reply=None):
    """-> {"intent", ...args, "ms"} or None when the router is not reachable."""
    ctx = [f"Обращение по имени: {'да' if named else 'нет'}",
           f"Идёт разговор с Евой: {'да' if in_dialog else 'нет'}",
           f"Музыка играет: {'да' if music_on else 'нет'}",
           f"На экране: {screen or 'её лицо'}"]
    if last_reply:
        ctx.append(f"Последняя реплика Евы: {last_reply[:200]}")
    body = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "\n".join(ctx) + f"\nФраза: «{text}»"}],
            "temperature": 0, "max_tokens": 80, "cache_prompt": True,
            "response_format": {"type": "json_object", "schema": SCHEMA}}
    t = time.time()
    try:
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.loads(json.loads(r.read())["choices"][0]["message"]["content"])
    except Exception as e:
        log("router error:", e)
        return None
    if out.get("intent") not in INTENTS:
        return None
    if not out.get("to_eva") and not (named or in_dialog):
        out["intent"] = "ignore"
    out["ms"] = int(1000 * (time.time() - t))
    return out
