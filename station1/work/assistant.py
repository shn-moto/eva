#!/usr/bin/env python3
"""Hybrid voice assistant for Yandex Station 1 (Debian, aarch64).

Pipeline: mic array (arecord, 5ch 16kHz S32) -> energy VAD -> Groq Whisper STT
-> Groq LLM (gpt-oss-120b) -> edge-tts (Russian neural voice) -> mpg123 -> speakers.

Config via environment or /opt/assistant/.env:
    GROQ_API_KEY   - required
    ASSISTANT_NAME - wake-name hint for the system prompt (default Алиса)
"""
import io
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
import collections

import numpy as np
import requests

# ---------- config ----------
def load_env(path="/opt/assistant/.env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
XAI_KEY = os.environ.get("XAI_API_KEY", "")
XAI_VOICE = os.environ.get("XAI_VOICE", "ara")
NAME = os.environ.get("ASSISTANT_NAME", "Ева")

GROQ_BASE = "https://api.groq.com/openai/v1"
STT_MODEL = "whisper-large-v3-turbo"
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
TTS_VOICE = "ru-RU-SvetlanaNeural"
# Активация — по имени в любом месте фразы, с любым обращением перед ним:
#   «Эй, Ева», «Слушай, Ева», «Скажи-ка, Ева», «Послушай, Ева»,
#   «Алло, Ева», «Ну, Ева», «Ева!» — всё это срабатывает одинаково:
#   команда = текст ПОСЛЕ имени, обращение до имени игнорируется.
# Варианты — падежи и типичные ошибки Whisper при распознавании имени.
WAKE_WORDS = ("ева", "эва", "ива", "eva", "еву", "евы", "евой", "евочка")
# Локальный tiny иногда слышит имя как «Гайева», «Алева» и т.п. —
# для таких случаев: токен, СОДЕРЖАЩИЙ «ева», считается именем, если он
# в начале/конце фразы или после слова-обращения. Частые ложные слова —
# в блок-листе.
VOCATIVES = ("эй", "э", "слушай", "послушай", "скажи", "скажи-ка", "скажика",
             "алло", "ну", "привет", "слышь", "ау")
WAKE_BLOCK = ("дерев", "нев", "андреев", "матвеев", "сергеев", "николаев",
              "васильев", "алексеев", "тимофеев", "леонтьев", "евгень",
              "европ", "евангел", "женева")
END_WORDS = ("спасибо", "всё", "все", "хватит", "пока", "стоп", "отстань",
             "конец", "до свидания", "свободен", "досвидания", "благодарю",
             "остановись", "останови", "замолчи", "молчи", "заткнись")
# Whisper noise artifacts: dropped before any wake/dialog logic
HALLUCINATIONS = ("субтитры", "dimatorzok", "продолжение следует",
                  "смотрите продолжение", "подписывайтесь", "ставьте лайк",
                  "всем пока", "всем привет", "до встречи", "редактор субтитров",
                  "корректор а.егорова", "перевод и озвуч")
DIALOG_TIMEOUT = 20.0  # seconds of silence that ends a dialog, quietly

REC_DEV = "hw:snddaudio0,0"
REC_RATE = 16000
REC_CH = 5
FRAME = 480  # 30 ms at 16 kHz

SYSTEM_PROMPT = (
    f"Ты — {NAME}, голосовой ассистент в умной колонке. Отвечай по-русски, "
    "без markdown и списков — текст будет озвучен. На обычные вопросы отвечай "
    "коротко (1-3 предложения), но если просят стихотворение, текст, сказку "
    "или историю — читай полностью, не обрывай. "
    "Если вопрос касается текущих событий, погоды, курсов, новостей, расписаний "
    "или фактов, в которых ты не уверен, — обязательно вызови web_search и "
    "отвечай по найденному, без выдумок. "
    "Если пользователь просит прочитать конкретное произведение, перевод, "
    "стихотворение или длинный текст — вызывай инструмент read_aloud, он "
    "найдёт текст и зачитает его; сам длинные тексты не декламируй. "
    "Если пользователь завершает беседу (стоп, хватит, остановись, всё, пока), "
    "ответь ровно одной фразой: «Хорошо, обращайтесь!» "
    "Если пользователь спрашивает, что он говорил, просил или обсуждал раньше "
    "(«что мы обсуждали», «напомни», «я говорил про», «о чём я просил») — "
    "вызывай memory_search: это семантический поиск по долговременной памяти "
    "всего, что он говорил вслух."
)

TOOLS = [{
    "name": "web_search",
    "description": "Поиск в интернете: свежие новости, погода, курсы, факты",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "поисковый запрос"}
        },
        "required": ["query"],
    },
}, {
    "name": "read_aloud",
    "description": ("Найти произведение (стихотворение, текст, сказку) в "
                    "интернете и зачитать его вслух пользователю напрямую со "
                    "страницы. Используй, когда просят прочитать конкретное "
                    "произведение или перевод — сам его не декламируй. "
                    "Формируй запрос так, чтобы найти САМ ТЕКСТ, а не статью "
                    "о нём: добавляй слова «текст», «слова», «читать»."),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "запрос: автор, название, переводчик"},
        },
        "required": ["query"],
    },
}, {
    "name": "memory_search",
    "description": ("Семантический поиск по долговременной памяти: всё, что "
                    "пользователь говорил вслух раньше. Используй, когда "
                    "спрашивают «что мы обсуждали», «напомни, что я просил», "
                    "«я говорил про…». Отвечай по найденному, не выдумывай."),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "тема поиска своими словами"},
        },
        "required": ["query"],
    },
}]


_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _extract_text(html):
    """Самый текстонасыщенный блок страницы (эвристика для стихов/прозы)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer",
                     "nav", "form", "aside"]):
        tag.decompose()
    best, best_len = "", 0
    for div in soup.find_all(["div", "article", "section", "td", "main"]):
        ps = div.find_all("p", recursive=True)
        if not ps:
            continue
        txt = "\n".join(p.get_text(" ", strip=True) for p in ps)
        if len(txt) > best_len:
            best, best_len = txt, len(txt)
    if best_len < 400:
        best = soup.get_text("\n", strip=True)
    return best


# Подсказки «ищется произведение, а не статья о нём»
WORK_HINTS = ("стих", "сказк", "песн", "слова песни", "текст песни",
              "произведение", "басн", "читай", "прочитай", "рассказ")
# Энциклопедии — в самый конец при поиске произведений
ENCYCLOPEDIAS = ("wikipedia.", "wikiquote.", "wiktionary.")


def fetch_text(query, max_chars=8000):
    """Найти страницу по запросу и вытащить из неё основной текст.
    Для произведений энциклопедии (вики о происхождении песни и т.п.) —
    только в последнюю очередь."""
    from ddgs import DDGS
    try:
        hits = DDGS().text(query, max_results=8)
    except Exception as e:
        return None, f"Ошибка поиска: {e}"
    hits = hits or []
    if any(hint in query.lower() for hint in WORK_HINTS):
        plain = [h for h in hits
                 if not any(e in (h.get("href") or h.get("url") or "")
                            for e in ENCYCLOPEDIAS)]
        hits = plain + [h for h in hits if h not in plain]
    for h in hits:
        url = h.get("href") or h.get("url") or ""
        if not url.startswith("http"):
            continue
        try:
            r = requests.get(url, headers=_UA, timeout=15)
            if r.status_code != 200 or len(r.text) < 2000:
                continue
            txt = _extract_text(r.text)
            if len(txt) >= 400:
                return txt[:max_chars], url
        except Exception:
            continue
    return None, "Не удалось вытащить текст со страниц."


_READ_MORE = {"parts": []}  # непрочитанные куски read_aloud
CONTINUE_WORDS = ("дальше", "продолжай", "продолжить", "продолжение",
                  "читай дальше", "давай дальше")


def read_aloud(query, proc, noise):
    """Найти текст и зачитать его TTS по кускам. True если оборвали.
    Книги/рассказы читаются подряд без пауз (стоп — кнопка или голос),
    короткие тексты — порциями по ~3 минуты с «читай дальше»."""
    _READ_MORE["parts"] = []
    is_book = any(w in query.lower()
                  for w in ("книг", "роман", "повесть", "рассказ"))
    text, src = fetch_text(query, max_chars=40000 if is_book else 8000)
    if not text:
        log(f"read_aloud: не нашлось ({src})")
        say("К сожалению, не удалось найти читаемый текст.", proc, noise)
        return False
    log(f"read_aloud: {len(text)} символов из {src}")
    # режем на куски по ~450 символов на границах предложений
    import re
    parts, buf = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+", text):
        if len(buf) + len(sent) > 450 and buf:
            parts.append(buf)
            buf = ""
        buf += sent + " "
    if buf.strip():
        parts.append(buf)
    portion = len(parts) if is_book else 8  # ~3600 символов ≈ 3 минуты
    for part in parts[:portion]:
        if say(part.strip(), proc, noise):
            return True
    _READ_MORE["parts"] = parts[portion:]
    if _READ_MORE["parts"]:
        say("Это была первая часть. Скажите «читай дальше», если хотите "
            "продолжение.", proc, noise)
    return False


def read_more(proc, noise):
    """Следующая порция непрочитанного текста. True если оборвали."""
    parts, _READ_MORE["parts"] = _READ_MORE["parts"][:8], _READ_MORE["parts"][8:]
    for part in parts:
        if say(part.strip(), proc, noise):
            _READ_MORE["parts"] = []
            return True
    if _READ_MORE["parts"]:
        say("Скажите «читай дальше» для продолжения.", proc, noise)
    return False


def web_search(query, max_results=5):
    from ddgs import DDGS
    try:
        hits = DDGS().text(query, max_results=max_results)
        if not hits:
            return "Ничего не найдено."
        return "\n".join(
            f"- {h.get('title','')}: {h.get('body','')}" for h in hits)
    except Exception as e:
        return f"Ошибка поиска: {e}"


# ---------- долговременная память: семантический поиск (shn-linux) ----------
MEMORY_URL = os.environ.get("MEMORY_URL", "http://192.168.1.107:8903/search")


def memory_search(query, limit=5):
    """Что пользователь говорил раньше: эмбеддинг + pgvector на shn-linux."""
    try:
        r = requests.post(MEMORY_URL, json={"query": query, "limit": limit},
                          timeout=30)
        r.raise_for_status()
        rows = r.json().get("results", [])
    except Exception as e:
        log(f"memory_search error: {e}")
        return "Память сейчас недоступна."
    if not rows:
        return "В долговременной памяти ничего похожего не нашлось."
    return "Найдено в памяти (дата: сказанное):\n" + "\n".join(
        f"- {row['ts']}: {row['text']}" for row in rows)

# ---------- LED ring ----------
# /dev/ledring: кадр = 72 байта (24 RGB-светодиода), запись = отправка кадра.
class LedRing:
    FPS = 20

    def __init__(self, dev="/dev/ledring", n=24):
        self.n = n
        self.mode = "off"
        self._flash_until = 0.0
        self._lock = threading.Lock()
        try:
            self._fd = os.open(dev, os.O_WRONLY)
        except OSError as e:
            log(f"LED-кольцо недоступно: {e}")
            self._fd = None
        self._run = self._fd is not None
        if self._run:
            threading.Thread(target=self._loop, daemon=True).start()

    def set(self, mode):
        with self._lock:
            self.mode = mode

    def flash(self, seconds=0.35):
        self._flash_until = time.time() + seconds

    def close(self):
        self._run = False
        if self._fd is not None:
            try:
                os.write(self._fd, bytes(self.n * 3))
                os.close(self._fd)
            except OSError:
                pass

    def _loop(self):
        while self._run:
            try:
                os.write(self._fd, self._render(time.time()))
            except OSError:
                pass
            time.sleep(1.0 / self.FPS)

    def _render(self, t):
        with self._lock:
            mode = self.mode
        if t < self._flash_until:          # вспышка при активации
            return bytes([180, 180, 180] * self.n)
        if mode == "listen":               # «дыхание» цианом — слушает
            v = int(45 + 40 * math.sin(t * 3.0))
            return bytes([0, v, min(v + 30, 255)] * self.n)
        if mode == "think":                # бегущий огонёк — думает/ищет
            head = int(t * 14) % self.n
            frame = []
            for i in range(self.n):
                d = (head - i) % self.n
                v = 200 if d == 0 else 90 if d == 1 else 30 if d == 2 else 0
                frame += [v, v, v]
            return bytes(frame)
        if mode == "speak":                # ровное тёплое свечение — говорит
            return bytes([50, 42, 30] * self.n)
        return bytes(self.n * 3)           # off


LED = None  # инициализируется в main()


# ---------- физическая кнопка «стоп» ----------
# gpio-keys на Станции 1: любое нажатие (включая кнопку мьюта микрофона)
# мгновенно обрывает озвучку — аварийный стоп без шнура питания.
STOP = threading.Event()


def _stop_watcher(dev="/dev/input/event1"):
    try:
        fd = os.open(dev, os.O_RDONLY)
    except OSError as e:
        log(f"кнопка стоп недоступна ({dev}): {e}")
        return
    while True:
        try:
            data = os.read(fd, 24)  # struct input_event на aarch64
            if len(data) == 24:
                _, _, ev_type, _, value = struct.unpack("qqHHi", data)
                if ev_type in (1, 5) and value == 1:  # KEY press / SW вкл
                    STOP.set()
        except OSError:
            time.sleep(5)


# ---------- audio capture ----------
def open_mic():
    return subprocess.Popen(
        ["arecord", "-D", REC_DEV, "-c", str(REC_CH), "-f", "S32_LE",
         "-r", str(REC_RATE), "-t", "raw", "-q"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def read_frame(proc):
    raw = proc.stdout.read(FRAME * REC_CH * 4)
    if len(raw) < FRAME * REC_CH * 4:
        return None
    a = np.frombuffer(raw, dtype=np.int32).reshape(-1, REC_CH)
    mono = a.mean(axis=1)  # downmix array
    return mono


def calibrate(proc, seconds=1.0):
    """Measure ambient noise RMS."""
    vals = []
    for _ in range(int(seconds / 0.03)):
        f = read_frame(proc)
        if f is not None:
            vals.append(np.sqrt((f.astype(np.float64) ** 2).mean()))
    return float(np.median(vals)) if vals else 1e6


def listen_utterance(proc, noise_state, max_sec=10.0, silence_sec=0.75,
                     timeout_sec=30.0, duck_music=False):
    """Collect one utterance after voice onset. Returns int16 np array or None.
    noise_state — одноэлементный список с уровнем фона: медленно адаптируется
    по тихим кадрам, чтобы телевизор/улица не делали колонку глухой.
    Тихие слова не теряем: pre-roll 0.3 с перед началом фразы и конец фразы
    считаем от пика самой фразы, а не только от фона.
    duck_music=True: пока играет музыка, порог чуть мягче, но требуется
    устойчивая речь (3 кадра, барабанные доли отсеиваются), а при старте
    фразы музыка встаёт на паузу и возобновится при следующем прослушивании."""
    # музыка была автоматически приглушена на прошлой фразе — возвращаем
    if MUSIC.pop("_ducked", False) and music_active() and MUSIC["paused"]:
        music_pause(False)
        log("music: возобновлена после прослушивания")
    buf, sil, spoken = [], 0.0, 0.0
    waited = 0.0
    preroll = collections.deque(maxlen=25)  # 0.75 с до порога — мягкие начала
    soft_run = 0  # кадры чуть ниже порога подряд — мягкий старт речи
    sustain = 0  # подряд громких кадров поверх музыки
    mfloor = 0.0  # скользящий уровень самой музыки в микрофоне
    peak = 0.0
    while True:
        f = read_frame(proc)
        if f is None:
            return None
        rms = np.sqrt((f.astype(np.float64) ** 2).mean())
        music_on = duck_music and music_active() and not MUSIC["paused"]
        if music_on:
            # фоном служит сама музыка (быстро подстраиваемся, тау ~0.2 с),
            # речь должна быть заметно громче её текущего уровня
            mfloor = rms if mfloor == 0.0 else 0.85 * mfloor + 0.15 * rms
            onset_thresh = max(mfloor * 2.2, 3e5)
        else:
            mfloor = 0.0
            onset_thresh = max(noise_state[0] * 2.5, 3e5)
        if not buf:
            waited += 0.03
            if waited > timeout_sec:
                return None
            preroll.append(f)
            if rms > onset_thresh:
                sustain += 1
                if music_on and sustain < 5:
                    continue  # короткая доля музыки — не речь
                soft_run = 0
                buf.extend(preroll)  # захватить тихое начало фразы
                preroll.clear()
                peak = rms
                if music_on:
                    music_pause(True)
                    MUSIC["_ducked"] = True
                    log("music: приглушена на время прослушивания")
            elif not music_on and rms > onset_thresh * 0.55:
                # тихое начало речи: несколько тёплых кадров подряд — стартуем,
                # не дожидаясь громкого слога (иначе съедаются первые слова)
                # (при играющей музыке отключено — даёт ложные срабатывания)
                soft_run += 1
                if soft_run >= 3:
                    soft_run = 0
                    buf.extend(preroll)
                    preroll.clear()
                    peak = rms
            else:
                soft_run = 0
                sustain = 0
                if rms < onset_thresh * 0.8:
                    # тихий кадр — подстраиваем фон (тау ~1.5 с)
                    noise_state[0] = 0.98 * noise_state[0] + 0.02 * rms
        else:
            buf.append(f)
            peak = max(peak, rms)
            # «тихо» — относительно фона И пика фразы: тихие слова и
            # окончания держат фразу, длинная пауза — завершает
            speech_floor = max(noise_state[0] * 1.3, peak * 0.07, 1.2e5)
            if rms > speech_floor:
                sil = 0.0
            else:
                sil += 0.03
            spoken += 0.03
            if sil >= silence_sec or spoken >= max_sec:
                break
    if spoken < 0.5:
        return None
    log(f"VAD: длит={spoken:.1f}с пик={peak:.0f} фон={noise_state[0]:.0f}")
    mono = np.concatenate(buf)
    peak = np.abs(mono).max() or 1
    pcm16 = (mono / peak * min(peak / (2 ** 23), 1.0) * 0.9 * 32767).astype(np.int16)
    return pcm16


# ---------- cloud calls ----------
STT_BACKEND = os.environ.get("STT_BACKEND", "hybrid")  # hybrid | local | groq
LAN_STT_URL = os.environ.get("LAN_STT_URL", "http://192.168.1.107:8901/stt")
LOCAL_STT_MODEL = os.environ.get("LOCAL_STT_MODEL", "tiny")
_STT_TIMES = []            # локальный лимитер: максимум вызовов STT в минуту
_STT_BACKOFF_UNTIL = 0.0   # пауза после 429 от Groq
_STT_ERR_LOGGED = 0.0
_STT_DAY = ""              # дневной бюджет (защита лимита Groq 2000 RPD)
_STT_DAY_COUNT = 0
STT_MAX_PER_MIN = 6
STT_DAILY_BUDGET = 1800


def wav_bytes(pcm16):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(REC_RATE)
        w.writeframes(pcm16.tobytes())
    return b.getvalue()


_FW = None  # ленивая загрузка локальной модели


def stt_local(pcm16):
    """Локальный faster-whisper (int8). Пустая строка = ничего не расслышал."""
    global _FW
    if _FW is None:
        from faster_whisper import WhisperModel
        t0 = time.time()
        _FW = WhisperModel(LOCAL_STT_MODEL, device="cpu", compute_type="int8",
                           cpu_threads=4)
        log(f"локальная модель {LOCAL_STT_MODEL} загружена за {time.time()-t0:.1f}с")
    audio = pcm16.astype(np.float32) / 32768.0
    segs, _ = _FW.transcribe(audio, language="ru", beam_size=1,
                             vad_filter=True, condition_on_previous_text=False)
    return " ".join(s.text for s in segs).strip()


def stt_lan(pcm16):
    """GigaAM на shn-linux: шлём сырой PCM16 16kHz, получаем текст."""
    r = requests.post(LAN_STT_URL, data=pcm16.tobytes(),
                      headers={"Content-Type": "application/octet-stream"},
                      timeout=20)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def stt(pcm16):
    """STT: hybrid — LAN (GigaAM на shn-linux) -> Groq -> локальный tiny.
    Пустой ответ от доступного движка = «не расслышано», эскалации нет;
    фолбэк только при ошибке/недоступности движка."""
    if STT_BACKEND == "groq":
        return stt_groq(pcm16)
    if STT_BACKEND == "local":
        try:
            text = stt_local(pcm16)
            if text:
                return text
        except Exception as e:
            log(f"local STT error: {e}")
        return stt_groq(pcm16)
    # hybrid
    if LAN_STT_URL:
        try:
            return stt_lan(pcm16)
        except Exception as e:
            log(f"lan STT error: {e}")
    if time.time() < _STT_BACKOFF_UNTIL:  # квота Groq исчерпана — сразу локально
        try:
            return stt_local(pcm16)
        except Exception as e:
            log(f"local STT error: {e}")
            return ""
    try:
        return stt_groq(pcm16)
    except Exception as e:
        log(f"groq STT error: {e}")
        try:
            return stt_local(pcm16)
        except Exception as e2:
            log(f"local STT error: {e2}")
            return ""


def stt_groq(pcm16):
    """Whisper через Groq, с защитой квоты: локальный лимитер и backoff на 429."""
    global _STT_BACKOFF_UNTIL, _STT_ERR_LOGGED, _STT_DAY, _STT_DAY_COUNT
    now = time.time()
    if now < _STT_BACKOFF_UNTIL:
        return ""
    _STT_TIMES[:] = [t for t in _STT_TIMES if now - t < 60]
    if len(_STT_TIMES) >= STT_MAX_PER_MIN:
        return ""  # слишком много срабатываний — почти наверняка шум
    today = time.strftime("%Y-%m-%d")
    if today != _STT_DAY:
        _STT_DAY, _STT_DAY_COUNT = today, 0
    if _STT_DAY_COUNT >= STT_DAILY_BUDGET:
        if now - _STT_ERR_LOGGED > 3600:
            _STT_ERR_LOGGED = now
            log("STT: исчерпан дневной бюджет вызовов, до завтра только тишина")
        return ""
    _STT_TIMES.append(now)
    _STT_DAY_COUNT += 1
    try:
        r = requests.post(
            f"{GROQ_BASE}/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            files={"file": ("speech.wav", wav_bytes(pcm16), "audio/wav")},
            data={"model": STT_MODEL, "language": "ru", "response_format": "json"},
            timeout=30,
        )
        r.raise_for_status()
        _STT_BACKOFF_UNTIL = 0.0
        return r.json().get("text", "").strip()
    except requests.HTTPError as e:
        resp = e.response
        if resp is not None and resp.status_code == 429:
            try:
                wait = min(float(resp.headers.get("Retry-After", "")), 300.0)
            except ValueError:
                wait = 60.0
            _STT_BACKOFF_UNTIL = time.time() + max(wait, 10.0)
            if now - _STT_ERR_LOGGED > 300:
                _STT_ERR_LOGGED = now
                log(f"STT: лимит Groq (429), пауза {max(wait, 10.0):.0f}с "
                    "(шумовые вызовы в это время пропускаются)")
            return ""
        raise


RU_DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница",
           "суббота", "воскресенье")


def now_str():
    """Текущие дата/время в поясе пользователя — системные часы колонки
    живут в MSK, а пользователь в Europe/Warsaw; LLM нужно его «сейчас»."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        now = datetime.now(ZoneInfo(os.environ.get("ASSISTANT_TZ",
                                                   "Europe/Warsaw")))
        return f"{now:%H:%M}, {RU_DAYS[now.weekday()]}, {now:%d.%m.%Y}"
    except Exception:
        lt = time.localtime()
        return time.strftime(f"%H:%M, {RU_DAYS[lt.tm_wday]}, %d.%m.%Y", lt)


def llm(history, system, proc=None, noise=0.0, rounds=4):
    """Claude (Anthropic Messages API) с инструментами web_search/read_aloud."""
    for _ in range(rounds):
        r = requests.post(
            f"{ANTHROPIC_BASE}/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": LLM_MODEL, "max_tokens": 1600,
                  "system": system, "messages": history, "tools": TOOLS,
                  "thinking": {"type": "between_tools"}},
            timeout=60,
        )
        r.raise_for_status()
        d = r.json()
        blocks = d.get("content", [])
        history.append({"role": "assistant", "content": blocks})
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            return "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text").strip()
        results = []
        for u in uses:
            q = (u.get("input") or {}).get("query", "")
            if u["name"] == "web_search":
                log(f"SEARCH: {q!r}")
                result = web_search(q)
            elif u["name"] == "memory_search":
                log(f"MEMORY: {q!r}")
                result = memory_search(q)
            elif u["name"] == "read_aloud":
                log(f"READ_ALOUD: {q!r}")
                if read_aloud(q, proc, noise):
                    result = ("Чтение прервано пользователем. Он попросил "
                              "остановиться — ответь ровно одной фразой: "
                              "«Хорошо, обращайтесь!»")
                else:
                    result = "Текст зачитан пользователю вслух."
            else:
                result = "Неизвестный инструмент."
            results.append({"type": "tool_result", "tool_use_id": u["id"],
                            "content": result[:3000]})
        history.append({"role": "user", "content": results})
    return "Не удалось получить ответ, спросите ещё раз."


def _tts_xai(text, mp3):
    r = requests.post(
        "https://api.x.ai/v1/tts",
        headers={"Authorization": f"Bearer {XAI_KEY}",
                 "Content-Type": "application/json"},
        json={"text": text, "voice_id": XAI_VOICE, "language": "ru"},
        timeout=40,
    )
    r.raise_for_status()
    with open(mp3, "wb") as f:
        f.write(r.content)


def _tts_edge(text, mp3):
    import edge_tts
    import asyncio

    async def _gen():
        await edge_tts.Communicate(text, TTS_VOICE).save(mp3)

    asyncio.run(_gen())


TTS_CACHE_DIR = "/opt/assistant/tts_cache"
TTS_PREWARM = ("Слушаю!", "Хорошо, обращайтесь!", "Пауза.",
               "Играю дальше.", "Музыка выключена.", "Что включить?",
               "Ещё одну фразу.")


def _tts_generate(text, mp3):
    try:
        if not XAI_KEY:
            raise RuntimeError("нет XAI_API_KEY")
        _tts_xai(text, mp3)
    except Exception as e:
        log(f"xAI TTS недоступен ({e}), фолбэк на edge-tts")
        _tts_edge(text, mp3)


def _tts_cached(text):
    """Путь к mp3: из кэша или свежесгенерированный (и положен в кэш)."""
    import hashlib
    key = hashlib.md5(f"{XAI_VOICE}|{text}".encode()).hexdigest()[:16]
    path = os.path.join(TTS_CACHE_DIR, key + ".mp3")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    _tts_generate(text, tmp)
    os.replace(tmp, path)
    # кэш ограничен: не более 200 файлов, старейшие удаляются
    try:
        files = sorted(
            (os.path.join(TTS_CACHE_DIR, f) for f in os.listdir(TTS_CACHE_DIR)
             if f.endswith(".mp3")),
            key=os.path.getmtime)
        for f in files[:-200]:
            os.unlink(f)
    except OSError:
        pass
    return path


def tts_prewarm():
    """Фон: заранее озвучить служебные фразы, чтобы игрались мгновенно."""
    for t in TTS_PREWARM:
        try:
            _tts_cached(t)
        except Exception as e:
            log(f"tts prewarm: {e}")


def say(text, proc=None, noise=0.0):
    """Озвучить текст. Прерывание: физическая кнопка на колонке (всегда)
    или громкая речь поверх звука (barge-in), если передан микрофон proc."""
    interrupted = False
    prev_mode = LED.mode if LED else "off"
    if LED:
        LED.set("speak")
    STOP.clear()  # игнорируем нажатия до начала озвучки
    was_playing = music_active() and not MUSIC["paused"]
    if was_playing:
        music_pause(True)  # музыка в паузу на время речи ассистента
    try:
        mp3 = _tts_cached(text)
        p = subprocess.Popen(["mpg123", "-q", mp3])
        # базовый уровень = собственное звучание в микрофонах
        base, n = 0.0, 0
        if proc is not None and noise > 0:
            while p.poll() is None and n < 16:  # ~0.5 с замера
                if STOP.is_set():
                    break
                f = read_frame(proc)
                if f is None:
                    break
                base += np.sqrt((f.astype(np.float64) ** 2).mean())
                n += 1
            base = base / n if n else 0.0
        thresh = max(base * 1.35, noise * 3.5, 2.5e5)
        loud = 0
        while p.poll() is None:
            if STOP.is_set():
                p.terminate()
                interrupted = True
                log("стоп: нажата кнопка на колонке")
                break
            if proc is None or noise <= 0:
                time.sleep(0.05)
                continue
            f = read_frame(proc)
            if f is None:
                break
            rms = np.sqrt((f.astype(np.float64) ** 2).mean())
            loud = loud + 1 if rms > thresh else 0
            if loud >= 8:  # ~0.24 с уверенной речи поверх звука
                p.terminate()
                interrupted = True
                log("barge-in: озвучка оборвана пользователем")
                break
        p.wait()
    finally:
        # mp3 живёт в кэше — не удаляем
        if LED:
            LED.set(prev_mode)
        if was_playing:
            music_pause(False)  # вернуть музыку после ответа
    return interrupted


# ---------- transcript (долговременная память) ----------
TRANSCRIPT_DIR = "/opt/assistant/transcript"


def transcript_write(role, text, dialog=False, woke=False, speaker=None,
                     stt_backend=None):
    """Долговременная стенограмма: JSONL по дням, переживает перезагрузки.
    Формат строки расширяемый — позже сюда лягут embedding и vector id."""
    try:
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "role": role, "text": text}
        if role == "user":
            rec.update({"dialog": bool(dialog), "woke": bool(woke),
                        "speaker": speaker, "stt": stt_backend})
        day = time.strftime("%Y-%m-%d")
        with open(os.path.join(TRANSCRIPT_DIR, day + ".jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"transcript write error: {e}")


# ---------- speaker ID (CAM++ через sherpa-onnx) ----------
SPK_MODEL = "/opt/assistant/models/campplus.onnx"
SPK_PROFILES_FILE = "/opt/assistant/speakers.json"
SPK_CLUSTERS_FILE = "/opt/assistant/speaker_clusters.json"
SPK_THRESHOLD = float(os.environ.get("SPK_THRESHOLD", "0.5"))
CLUSTER_MATCH = float(os.environ.get("CLUSTER_MATCH", "0.62"))
CLUSTER_MAX_EMB = 10        # сколько образцов держим на кластер-центроид
CLUSTER_MIN_SEC = 1.2       # короткие вскрики не кластеризуем
_SPK = None
_SPK_PROFILES = {}
_SPK_CLUSTERS = {}


def _load_profiles():
    global _SPK_PROFILES
    if os.path.exists(SPK_PROFILES_FILE):
        with open(SPK_PROFILES_FILE, encoding="utf-8") as f:
            _SPK_PROFILES = json.load(f)
        log(f"speaker-профили: {sorted(_SPK_PROFILES)}")


def _save_profiles():
    with open(SPK_PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(_SPK_PROFILES, f)


def _load_clusters():
    global _SPK_CLUSTERS
    if os.path.exists(SPK_CLUSTERS_FILE):
        try:
            with open(SPK_CLUSTERS_FILE, encoding="utf-8") as f:
                _SPK_CLUSTERS = json.load(f)
            if _SPK_CLUSTERS:
                log(f"голосовые кластеры: "
                    f"{[(k, v.get('count', 0)) for k, v in _SPK_CLUSTERS.items()]}")
        except Exception:
            _SPK_CLUSTERS = {}


def _save_clusters():
    with open(SPK_CLUSTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(_SPK_CLUSTERS, f)


def speaker_embed(pcm16):
    """Эмбеддинг голоса (CAM++) или None при ошибке."""
    global _SPK
    try:
        if _SPK is None:
            import sherpa_onnx
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=SPK_MODEL, num_threads=4)
            _SPK = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            _load_profiles()
            _load_clusters()
        audio = pcm16.astype(np.float32) / 32768.0
        st = _SPK.create_stream()
        st.accept_waveform(16000, audio)
        st.input_finished()
        emb = np.array(_SPK.compute(st), dtype=np.float32)
        return emb / (np.linalg.norm(emb) + 1e-9)
    except Exception as e:
        log(f"speaker_id error: {e}")
        return None


def match_speaker(emb):
    """(имя, similarity) по известным профилям; 'unknown' если нет совпадения."""
    if not _SPK_PROFILES:
        return "unknown", 0.0
    best, best_sim = None, -1.0
    for name, profs in _SPK_PROFILES.items():
        for p in profs:
            sim = float(np.dot(emb, np.asarray(p, dtype=np.float32)))
            if sim > best_sim:
                best, best_sim = name, sim
    label = best if best_sim >= SPK_THRESHOLD else "unknown"
    return label, round(best_sim, 3)


def cluster_assign(emb):
    """Неизвестный голос -> стабильный кластер «гость-N».
    Когда голос потом назовут («запомни голос Марина»), кластер сольётся
    в профиль — и прошлые реплики этого человека получат имя."""
    best, best_sim = None, -1.0
    for name, cl in _SPK_CLUSTERS.items():
        for p in cl["embs"]:
            sim = float(np.dot(emb, np.asarray(p, dtype=np.float32)))
            if sim > best_sim:
                best, best_sim = name, sim
    if best is not None and best_sim >= CLUSTER_MATCH:
        cl = _SPK_CLUSTERS[best]
        cl["count"] = cl.get("count", 0) + 1
        cl["last"] = time.time()
        # разнообразим центроид, но не плодим дубликаты
        if len(cl["embs"]) < CLUSTER_MAX_EMB and best_sim < 0.85:
            cl["embs"].append(emb.tolist())
        _save_clusters()
        return best, round(best_sim, 3)
    n = 1
    while f"гость-{n}" in _SPK_CLUSTERS:
        n += 1
    name = f"гость-{n}"
    _SPK_CLUSTERS[name] = {"embs": [emb.tolist()], "count": 1,
                           "first": time.time(), "last": time.time()}
    _save_clusters()
    log(f"новый голосовой кластер: {name}")
    return name, 0.0


# --- голосовое обучение: «Ева, запомни голос Марина» ---
ENROLL = {"name": None, "embs": [], "until": 0.0}
ENROLL_NEED = 3      # фраз для надёжного профиля
ENROLL_TIMEOUT = 60.0


def start_enroll(name):
    ENROLL.update(name=name, embs=[], until=time.time() + ENROLL_TIMEOUT)


def finish_enroll():
    """Сохранить собранные эмбеддинги в профиль; слить подходящие кластеры.
    Возвращает (имя, число_образцов, список_слитых_кластеров)."""
    name, embs = ENROLL["name"], ENROLL["embs"]
    ENROLL.update(name=None, embs=[])
    if not embs:
        return name, 0, []
    _SPK_PROFILES.setdefault(name, []).extend(e.tolist() for e in embs)
    moved = []
    for cname in list(_SPK_CLUSTERS):
        cl = _SPK_CLUSTERS[cname]
        hit = any(max(float(np.dot(e, np.asarray(p, dtype=np.float32)))
                      for e in embs) >= SPK_THRESHOLD for p in cl["embs"])
        if hit:
            _SPK_PROFILES[name].extend(cl["embs"])
            moved.append(cname)
            del _SPK_CLUSTERS[cname]
    _save_profiles()
    if moved:
        _save_clusters()
    log(f"enroll: профиль {name} += {len(embs)} образцов, слиты кластеры: {moved}")
    return name, len(embs), moved


# ---------- shipper: стенограмма -> Postgres на shn-linux ----------
PG_DSN = os.environ.get("ASSISTANT_PG_DSN", "")
SHIP_STATE = "/opt/assistant/.shipper_state"


def shipper_loop():
    """Хвост транскриптов -> Postgres. Закладка на диске переживает рестарты,
    при недоступной базе догоняет позже. Дедуп: ON CONFLICT DO NOTHING."""
    if not PG_DSN:
        log("shipper: ASSISTANT_PG_DSN не задан, доставка в БД отключена")
        return
    import psycopg
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(PG_DSN, autocommit=True,
                                       connect_timeout=5)
                log("shipper: подключено к assistant_memory")
            state = {}
            if os.path.exists(SHIP_STATE):
                with open(SHIP_STATE, encoding="utf-8") as f:
                    state = json.load(f)
            dirty = False
            for fn in sorted(os.listdir(TRANSCRIPT_DIR)):
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(TRANSCRIPT_DIR, fn)
                size = os.path.getsize(path)
                off = state.get(fn, 0)
                if off > size:
                    off = 0  # файл пересоздан
                if size == off:
                    continue
                with open(path, encoding="utf-8") as f:
                    f.seek(off)
                    lines = f.readlines()
                    state[fn] = f.tell()
                for line in lines:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    conn.execute(
                        "INSERT INTO utterances (ts, role, text, speaker,"
                        " dialog, woke, stt) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                        " ON CONFLICT DO NOTHING",
                        (rec.get("ts"), rec.get("role"), rec.get("text"),
                         rec.get("speaker"), rec.get("dialog"),
                         rec.get("woke"), rec.get("stt")))
                dirty = True
            if dirty:
                with open(SHIP_STATE, "w", encoding="utf-8") as f:
                    json.dump(state, f)
            time.sleep(5)
        except Exception as e:
            log(f"shipper: {e}")
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(15)


# ---------- музыка: YouTube через yt-dlp -> mpg123 стрим ----------
import shutil
YTDLP = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"
FFMPEG = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
MUSIC = {"proc": None, "ff": None, "dl": None,
         "title": None, "paused": False, "pending": False,
         "queue": [], "stop": False, "worker": None}


def music_active():
    return MUSIC["proc"] is not None and MUSIC["proc"].poll() is None


def music_stop():
    had = MUSIC["proc"] is not None or MUSIC["queue"]
    MUSIC["stop"] = True
    MUSIC["_ducked"] = False
    for key in ("proc", "ff", "dl"):
        p = MUSIC.get(key)
        if p is not None and p.poll() is None:
            p.terminate()
    if had:
        log(f"music: стоп ({MUSIC['title']!r})")
    MUSIC.update(proc=None, ff=None, dl=None, title=None,
                 paused=False, queue=[])


def music_pause(paused=True):
    p = MUSIC["proc"]
    if p is None or p.poll() is not None:
        return
    import signal
    try:
        os.kill(p.pid, signal.SIGSTOP if paused else signal.SIGCONT)
        MUSIC["paused"] = paused
    except OSError:
        pass


def music_volume(up):
    try:
        r = subprocess.run(["amixer", "-q", "sset", "Master",
                            ("5%+" if up else "5%-")],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            log(f"music volume: {(r.stderr or r.stdout)[:120]}")
    except Exception as e:
        log(f"music volume: {e}")


def music_control(low_text, proc, noise):
    """Управление музыкой без wake-word («стоп», «пауза», «тише»…).
    True = команда обработана."""
    if not music_active() and not MUSIC["paused"]:
        return False
    t = low_text
    if any(w in t for w in ("стоп", "выключи", "хватит")):
        music_stop()
        say("Музыка выключена.", proc, noise)
        return True
    if "пауза" in t:
        MUSIC["_ducked"] = False  # явная пауза — не возобновлять автоматически
        if not MUSIC["paused"]:
            music_pause(True)
        say("Пауза.", proc, noise)
        return True
    if any(w in t for w in ("следующ", "пропусти", "скип")):
        MUSIC["_ducked"] = False
        if MUSIC["queue"]:
            p = MUSIC["proc"]
            if p is not None and p.poll() is None:
                try:
                    import signal as _sig
                    os.kill(p.pid, _sig.SIGCONT)  # снять с паузы, иначе не умрёт
                except OSError:
                    pass
                p.terminate()
            say("Следующий трек.", proc, noise)
        else:
            say("Это единственный трек.", proc, noise)
        return True
    if any(w in t for w in ("продолжай", "дальше", "играй")) and MUSIC["paused"]:
        music_pause(False)
        say("Играю дальше.", proc, noise)
        return True
    if "тише" in t:
        music_volume(False)
        return True
    if "громче" in t:
        music_volume(True)
        return True
    return False


def _music_pipe(url):
    """Пайп yt-dlp -> ffmpeg -> mpg123.
    Статический ffmpeg не резолвит DNS — сеть делает yt-dlp,
    ffmpeg только декодирует webm/opus из stdin в mp3 для mpg123."""
    dl = subprocess.Popen([YTDLP, "-q", "-o", "-", url],
                          stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    ff = subprocess.Popen([FFMPEG, "-v", "error", "-i", "pipe:0",
                           "-f", "mp3", "-acodec", "libmp3lame",
                           "-b:a", "128k", "pipe:1"],
                          stdin=dl.stdout, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    dl.stdout.close()  # dl получит SIGPIPE, когда ff завершится
    mp = subprocess.Popen(["mpg123", "-q", "-"], stdin=ff.stdout,
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    ff.stdout.close()
    return mp, ff, dl


def _music_worker():
    """Играет очередь MUSIC['queue'] по одному треку (ленивый resolve)."""
    try:
        while MUSIC["queue"] and not MUSIC["stop"]:
            title, page_url = MUSIC["queue"][0]
            try:
                r = subprocess.run(
                    [YTDLP, "-f", "bestaudio", "--no-playlist", "-g",
                     page_url],
                    capture_output=True, text=True, timeout=60)
                direct = (r.stdout.strip().splitlines() or [""])[-1]
                if not direct:
                    raise RuntimeError((r.stderr or "")[:150])
            except Exception as e:
                log(f"music: пропуск {title!r}: {e}")
                MUSIC["queue"].pop(0)
                continue
            if MUSIC["stop"]:
                break
            mp, ff, dl = _music_pipe(direct)
            MUSIC.update(proc=mp, ff=ff, dl=dl, title=title, paused=False)
            log(f"music: играю {title!r}")
            while mp.poll() is None and not MUSIC["stop"]:
                time.sleep(0.5)
            if MUSIC["queue"]:
                MUSIC["queue"].pop(0)
        if not MUSIC["stop"]:
            log("music: очередь закончилась")
            MUSIC.update(proc=None, ff=None, dl=None,
                         title=None, paused=False)
    finally:
        MUSIC["worker"] = None


def _music_search_ytm(q, limit):
    """Поиск треков на YouTube Music -> [(title, watch_url), ...].
    Отбрасываем страницы артистов и плейлистов — только треки (watch?v=)."""
    import urllib.parse
    url = "https://music.youtube.com/search?q=" + urllib.parse.quote(q)
    r = subprocess.run([YTDLP, "--flat-playlist", "-J", url],
                       capture_output=True, text=True, timeout=120)
    j = json.loads(r.stdout)
    out = []
    for e in (j.get("entries") or []):
        u = e.get("url") or e.get("webpage_url") or ""
        t = e.get("title")
        if t and "watch?v=" in u:
            out.append((t, u))
        if len(out) >= limit:
            break
    if not out:
        raise RuntimeError("пустой результат YouTube Music")
    return out


def music_play(query, proc, noise):
    """Найти музыку и стримить через mpg123.
    Треки и подборки — YouTube Music; «альбом X» — длинный full-album
    ролик с YouTube. True = играет. При неудаче — режим уточнения запроса."""
    music_stop()
    LED.set("think")
    r = None
    try:
        if "альбом" in query.lower():
            q = re.sub(r"\bальбом\w*\b", " ", query.lower())
            q = " ".join(q.split())
            r = subprocess.run(
                [YTDLP, "-f", "bestaudio", "--no-playlist", "-J",
                 f"ytsearch1:{q} full album"],
                capture_output=True, text=True, timeout=90)
            j = json.loads(r.stdout)
            e = (j.get("entries") or [j])[0]
            entries = [(e.get("title") or q, e["webpage_url"])]
            announce = f"Играю альбом: {entries[0][0]}"
            log(f"music: альбом {q!r} -> {entries[0][0]!r}")
        else:
            multi = bool(re.search(
                r"подборк|плейлист|сборник|песни|треки", query.lower()))
            q = re.sub(
                r"\b(подборк\w*|плейлист\w*|сборник\w*|"
                r"песни|песен|треки|треков)\b", " ", query.lower())
            q = " ".join(q.split()) or query
            entries = _music_search_ytm(q, 5 if multi else 1)
            if multi:
                announce = (f"Играю подборку: {q}. "
                            f"Первый трек: {entries[0][0]}")
                log(f"music: подборка {q!r}, треков: {len(entries)}")
            else:
                announce = f"Играю: {entries[0][0]}"
                log(f"music: трек {entries[0][0]!r}")
    except Exception as ex:
        err = (r.stderr or "")[:200] if r is not None else ""
        log(f"music resolve error: {ex} {err}")
        say("Не удалось найти такой трек. Что поискать ещё?", proc, noise)
        MUSIC["pending"] = True
        return False
    MUSIC.update(queue=entries, stop=False, pending=False)
    say(announce, proc, noise)
    MUSIC["worker"] = threading.Thread(target=_music_worker, daemon=True)
    MUSIC["worker"].start()
    return True


# ---------- wake word ----------
def _tokens(text):
    return [w.strip(".,!?…:;\"'«»()-") for w in text.lower().split()]


def _is_wake_token(t, i, n, prev):
    if t in WAKE_WORDS:
        return True
    if "ева" not in t or any(t.startswith(b) for b in WAKE_BLOCK):
        return False
    # «Гайева», «Алева» и прочие огибки: имя в начале/конце фразы
    # или сразу после обращения («эй», «слушай», «скажи-ка»…)
    return i == 0 or i == n - 1 or prev in VOCATIVES


def extract_command(text):
    """Срабатывание по имени целым токеном в любом месте фразы.
    Обращение перед именем («эй», «слушай», «скажи-ка»…) игнорируется,
    команда = всё, что сказано после имени."""
    words = text.split()
    toks = _tokens(text)
    n = len(toks)
    for i, t in enumerate(toks):
        if _is_wake_token(t, i, n, toks[i - 1] if i else ""):
            cmd = " ".join(words[i + 1:]).lstrip(" ,.:;-")
            return cmd, True
    return text, False


def is_noise(text):
    low = text.lower().strip(".,!?… ")
    if not low:
        return True
    return any(h in low for h in HALLUCINATIONS)


def is_goodbye(text):
    """User clearly ended the conversation (short phrase from END_WORDS)."""
    words = _tokens(text)
    return 0 < len(words) <= 4 and any(w in END_WORDS for w in words)


def flush_mic(proc, seconds=1.5):
    """Drop audio captured while we were busy (own voice, processing time)."""
    import select
    fd = proc.stdout.fileno()
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            if not os.read(fd, 1 << 20):
                break


# ---------- main loop ----------
def main():
    global LED
    if not GROQ_KEY or not ANTHROPIC_KEY:
        sys.exit("Нужны GROQ_API_KEY и ANTHROPIC_API_KEY (см. /opt/assistant/.env)")
    log("калибровка шума...")
    proc = open_mic()
    noise = calibrate(proc)
    noise_state = [noise]  # адаптивный фон для listen_utterance
    log(f"шум RMS={noise:.0f}, порог начала речи={max(noise * 2.5, 3e5):.0f}")
    LED = LedRing()
    LED.flash(0.6)
    threading.Thread(target=_stop_watcher, daemon=True).start()
    threading.Thread(target=tts_prewarm, daemon=True).start()
    threading.Thread(target=shipper_loop, daemon=True).start()
    if STT_BACKEND in ("local", "hybrid"):
        try:
            stt_local(np.zeros(16000, dtype=np.int16))  # прогрев модели
        except Exception as e:
            log(f"прогрев локальной модели не удался: {e}")
    try:
        say(f"Ассистент {NAME} запущена.")
        flush_mic(proc)
        history = []  # Anthropic: system передаётся отдельным параметром
        dialog = False  # True = inside a conversation: no wake word needed
        dialog_log = False  # диалог принадлежит опознанному голосу — пишем ответы
        while True:
            system = f"{SYSTEM_PROMPT} Сейчас {now_str()}."  # свежее время
            LED.set("listen" if dialog else "off")
            utt = listen_utterance(
                proc, noise_state, timeout_sec=DIALOG_TIMEOUT if dialog else 30.0,
                duck_music=True)
            if utt is None:
                if ENROLL["name"] and time.time() > ENROLL["until"]:
                    name, cnt, _ = finish_enroll()
                    if cnt:
                        say(f"Готово, я запомнила голос {name}.",
                            proc, noise_state[0])
                    else:
                        say("Не расслышала речь, голос не сохранён.",
                            proc, noise_state[0])
                    flush_mic(proc)
                if dialog:
                    log("таймаут тишины — диалог завершён")
                    dialog = False
                    dialog_log = False
                    history.clear()
                continue
            try:
                if STOP.is_set() and (music_active() or MUSIC["paused"]):
                    STOP.clear()
                    music_stop()
                    log("музыка остановлена кнопкой на колонке")
                # speaker ID параллельно с STT — не тратим время зря
                spk_result = [None, 0.0, None]

                def _spk_job():
                    emb = speaker_embed(utt)
                    if emb is None:
                        return
                    label, sim = match_speaker(emb)
                    spk_result[:] = (label, sim, emb)

                spk_thread = threading.Thread(target=_spk_job, daemon=True)
                spk_thread.start()
                text = stt(utt)
                spk_thread.join(timeout=10)
                # режим обучения: фразы идут в профиль, а не в команды
                if ENROLL["name"]:
                    if spk_result[2] is not None:
                        ENROLL["embs"].append(spk_result[2])
                        if len(ENROLL["embs"]) >= ENROLL_NEED:
                            name, cnt, moved = finish_enroll()
                            extra = (f" Раньше я слышала этот голос как "
                                     f"{', '.join(moved)}.") if moved else ""
                            say(f"Готово, я запомнила голос {name}.{extra}",
                                proc, noise_state[0])
                        else:
                            say("Ещё одну фразу.", proc, noise_state[0])
                    flush_mic(proc)
                    continue
                if not text:
                    continue  # лимитер/429/пусто — тихо пропускаем
                log(f"STT: {text!r}{' [dialog]' if dialog else ''}")
                if len(text) < 2 or is_noise(text):
                    log("шум/галлюцинация, пропуск")
                    continue
                cmd, woke = extract_command(text)
                speaker, spk_sim, spk_emb = spk_result
                # управление музыкой — без wake-word, до записи в память
                if not dialog and music_control(text.lower(), proc,
                                                noise_state[0]):
                    flush_mic(proc)
                    continue
                # В долговременную память пишем только реплики опознанных
                # голосов (сейчас — Alex). Неопознанные/чужие голоса, детский
                # лепет и телевизор в базу не попадают; в лог — по-прежнему да.
                known = bool(speaker) and speaker != "unknown"
                if known:
                    transcript_write("user", text, dialog=dialog, woke=woke,
                                     speaker=speaker, stt_backend=STT_BACKEND)
                    dialog_log = True
                    log(f"speaker: {speaker} ({spk_sim})")
                elif spk_emb is not None and len(utt) > CLUSTER_MIN_SEC * REC_RATE:
                    # незнакомый голос — в кластер «гость-N»: когда его назовут,
                    # прошлые реплики человека получат имя
                    cname, csim = cluster_assign(spk_emb)
                    log(f"speaker: неизвестный → {cname} ({csim})")
                if not dialog and not woke:
                    log("нет wake-word, пропуск")
                    continue
                if woke:
                    LED.flash()
                if is_goodbye(cmd):
                    MUSIC["pending"] = False
                    if music_active() or MUSIC["paused"]:
                        music_stop()
                    say("Хорошо, обращайтесь!")
                    flush_mic(proc)
                    dialog = False
                    dialog_log = False
                    history.clear()
                    log("диалог завершён фразой выхода")
                    continue
                if woke and len(cmd) < 3 and not dialog:
                    # голое обращение «Ева!» — приглашаем к команде
                    say("Слушаю!")
                    flush_mic(proc)
                    dialog = True
                    continue
                # «запомни голос Марина» — голосовое обучение профиля
                m_enroll = re.match(
                    r"запомни\s+(?:мой\s+)?голос(?:\s+как)?\s+(.+)", cmd.lower())
                if woke and m_enroll:
                    name = m_enroll.group(1).strip(" .,!?").capitalize()
                    start_enroll(name)
                    say(f"Хорошо. Скажите {ENROLL_NEED} фразы, "
                        f"я запоминаю голос {name}.", proc, noise_state[0])
                    flush_mic(proc)
                    dialog = False
                    dialog_log = False
                    history.clear()
                    continue
                # «включи/поставь/сыграй …» — музыка с YouTube
                m_play = re.match(r"(?:включи|поставь|сыграй|вруби)\s+(.+)",
                                  cmd.lower())
                if m_play:
                    q = m_play.group(1).strip(" .,!?")
                    if q and q not in ("музыку", "музыка"):
                        ok = music_play(q, proc, noise_state[0])
                        dialog = not ok  # не нашлось — можно уточнить запрос
                        if ok:
                            history.clear()
                    elif MUSIC["paused"]:
                        music_pause(False)
                        say("Играю дальше.", proc, noise_state[0])
                        dialog = False
                    else:
                        say("Что включить?", proc, noise_state[0])
                        MUSIC["pending"] = True
                        dialog = True  # следующая фраза — название трека
                    flush_mic(proc)
                    dialog_log = False
                    continue
                if len(cmd) < 2:
                    continue
                low = cmd.lower().strip(".,!? ")
                # ждали уточнения запроса — эта фраза и есть запрос
                if MUSIC["pending"]:
                    MUSIC["pending"] = False
                    ok = music_play(cmd, proc, noise_state[0])
                    dialog = not ok
                    dialog_log = False
                    history.clear()
                    flush_mic(proc)
                    continue
                if _READ_MORE["parts"] and any(w in low for w in CONTINUE_WORDS):
                    if read_more(proc, noise_state[0]):
                        dialog = False
                        dialog_log = False
                        history.clear()
                    continue
                dialog = True
                LED.set("think")  # реальный запрос ушёл к модели
                history.append({"role": "user", "content": cmd})
                history[:] = history[-8:]
                reply = llm(history, system, proc, noise_state[0])
                log(f"LLM: {reply!r}")
                if dialog_log:
                    transcript_write("assistant", reply)
                interrupted = say(reply, proc, noise_state[0])
                if not interrupted:
                    flush_mic(proc)
                if "обращайтесь" in reply.lower() and len(reply) < 60:
                    dialog = False
                    dialog_log = False
                    history.clear()
                    log("диалог завершён моделью")
            except Exception as e:
                log(f"ERROR: {e}")
                time.sleep(1)
    finally:
        LED.close()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


if __name__ == "__main__":
    main()
