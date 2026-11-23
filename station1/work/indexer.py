#!/usr/bin/env python3
"""assistant-memory pipeline (shn-linux):

Stage 1 — SEGMENTER: сырые VAD-фрагменты из utterances -> логические куски
(реплики/тёрны) в таблицу segments. Мусор (шум, галлюцинации STT, петли,
бессмысленные обрывки) отбрасывается по эвристикам и в segments не попадает.

Правила склейки в один сегмент: та же роль, тот же спикер, тот же диалог,
пауза между фрагментами <= GAP_SEC, новый фрагмент не woke-обращение.

Stage 2 — CORRECTOR: пользовательские сегменты -> маленькая LLM на shn-linux
(llama.cpp server) — правит фонетические ошибки STT, не трогая смысл.
Ответы ассистента не корректируются (они и так чистые). Шум, который модель
вернула пустым, в поиск не попадает (не индексируется).

Stage 3 — EMBEDDER: segments без embedding -> fastembed (384d) -> UPDATE.
Эмбеддинг считается по исправленному тексту, если коррекция уже прошла.

Raw-таблица utterances остаётся нетронутым архивом; поиск идёт по segments.
"""
import os
import re
import json
import threading
import time

import psycopg

DSN = os.environ["ASSISTANT_PG_DSN"]
MODEL = os.environ.get("EMBED_MODEL",
                       "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
BATCH = int(os.environ.get("EMBED_BATCH", "32"))
GAP_SEC = float(os.environ.get("SEG_GAP_SEC", "8"))
SEG_BATCH = int(os.environ.get("SEG_BATCH", "500"))
CORRECT_URL = os.environ.get(
    "CORRECT_URL", "http://127.0.0.1:8902/v1/chat/completions")
CORRECT_BATCH = int(os.environ.get("CORRECT_BATCH", "4"))
MEM_PORT = int(os.environ.get("MEM_HTTP_PORT", "8903"))
MEM_MAX_DIST = float(os.environ.get("MEM_MAX_DIST", "0.8"))
# пояс, в котором показываем даты пользователю (БД хранит UTC)
MEM_TZ = os.environ.get("MEM_TZ", "Europe/Warsaw")
CORRECT_SYSTEM = (
    "Ты — корректор машинной расшифровки русской речи. Получаешь фрагмент, "
    "распознанный с ошибками (фонетические замены, склейки, лишние или "
    "пропущенные слова). Исправь очевидные ошибки распознавания, сохрани "
    "смысл, стиль и разговорность, ничего не добавляй от себя. Отвечай "
    "ТОЛЬКО исправленным текстом, без пояснений и кавычек. Если фрагмент — "
    "бессмысленный шум, верни пустую строку.")

embedder = None

# Галлюцинации Whisper на тишине/шуме (как на колонке + частые добавки)
HALLUC = ("субтитры", "dimatorzok", "продолжение следует",
          "смотрите продолжение", "подписывайтесь", "ставьте лайк",
          "всем пока", "всем привет", "до встречи", "редактор субтитров",
          "корректор а.егорова", "перевод и озвуч", "амедиатека",
          "озвучено", "сделано в", "music", "аплодисменты",
          "динамичная музыка", "спокойная музыка", "[музыка]")
VOWELS = set("аеёиоуыэюяaeiouy")

DDL = """
CREATE TABLE IF NOT EXISTS segments (
  id bigserial PRIMARY KEY,
  ts_start timestamptz NOT NULL,
  ts_end timestamptz NOT NULL,
  role text,
  speaker text,
  dialog boolean,
  woke boolean,
  text text,
  src_ids bigint[],
  embedding vector(384),
  created_at timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS segments_uniq
  ON segments (ts_start, role, md5(text));
CREATE INDEX IF NOT EXISTS segments_ts_idx ON segments (ts_start);
CREATE INDEX IF NOT EXISTS segments_speaker_idx ON segments (speaker);
CREATE TABLE IF NOT EXISTS segmenter_state (
  k text PRIMARY KEY,
  v bigint NOT NULL
);
ALTER TABLE segments ADD COLUMN IF NOT EXISTS corrected text;
"""


def is_junk(text):
    """Причина отбрасывания фрагмента или None (фрагмент валиден)."""
    t = text.strip()
    if len(t) < 2:
        return "short"
    low = t.lower().strip(".,!?… ")
    if not low:
        return "empty"
    if any(h in low for h in HALLUC):
        return "halluc"
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 2:
        return "noletters"
    nospace = len(t.replace(" ", ""))
    if nospace and len(letters) / nospace < 0.5:
        return "symbols"  # цифры/мусор вместо речи
    vow = sum(1 for c in letters if c.lower() in VOWELS)
    if vow / len(letters) < 0.15:
        return "novowels"  # «кшбрвл» — не распознанная речь
    words = low.split()
    run = 1
    for i in range(1, len(words)):
        run = run + 1 if words[i] == words[i - 1] else 1
        if run >= 5:
            return "loop"  # ASR-петля: слово повторяется подряд
    if len(words) >= 8 and len(set(words)) / len(words) < 0.25:
        return "repetitive"
    return None


def normalize(text):
    """Лёгкая нормализация склеенного сегмента (T9-подобная, без LLM)."""
    t = re.sub(r"\s+", " ", text).strip()
    if not t:
        return t
    t = t[0].upper() + t[1:]
    if t[-1] not in ".!?…":
        t += "."
    return t


def get_embedder():
    global embedder
    if embedder is None:
        from fastembed import TextEmbedding
        embedder = TextEmbedding(model_name=MODEL)
    return embedder


def ensure_schema(conn):
    for stmt in DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()


def get_bookmark(conn):
    row = conn.execute(
        "SELECT v FROM segmenter_state WHERE k = 'last_utterance_id'"
    ).fetchone()
    return row[0] if row else 0


def set_bookmark(conn, v):
    conn.execute(
        "INSERT INTO segmenter_state (k, v) VALUES ('last_utterance_id', %s)"
        " ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (v,))


def segment_pass(conn):
    """Один проход сегментатора. Возвращает (обработано, записано, мусор)."""
    last = get_bookmark(conn)
    rows = conn.execute(
        "SELECT id, ts, role, text, speaker, dialog, woke FROM utterances"
        " WHERE id > %s ORDER BY id LIMIT %s", (last, SEG_BATCH)).fetchall()
    if not rows:
        return 0, 0, 0

    seg = None          # накапливаемый сегмент
    written = junked = 0
    prev_text = ""

    def flush():
        nonlocal seg, written
        if not seg:
            return
        text = normalize(" ".join(seg["parts"]))
        if text:
            conn.execute(
                "INSERT INTO segments (ts_start, ts_end, role, speaker,"
                " dialog, woke, text, src_ids) VALUES"
                " (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (seg["ts_start"], seg["ts_end"], seg["role"], seg["speaker"],
                 seg["dialog"], seg["woke"], text, seg["src_ids"]))
            written += 1
        seg = None

    for rid, ts, role, text, speaker, dialog, woke in rows:
        reason = is_junk(text or "")
        if reason:
            junked += 1
            print(f"junk #{rid} [{reason}]: {(text or '')[:60]!r}", flush=True)
            continue
        if (text or "").strip() == prev_text:
            junked += 1  # дубль от повторного срабатывания VAD
            continue
        prev_text = (text or "").strip()

        same = (seg and seg["role"] == role and seg["speaker"] == speaker
                and seg["dialog"] == bool(dialog) and not woke
                and (ts - seg["ts_end"]).total_seconds() <= GAP_SEC)
        if not same:
            flush()
            seg = {"ts_start": ts, "role": role, "speaker": speaker,
                   "dialog": bool(dialog), "woke": bool(woke),
                   "parts": [], "src_ids": []}
        seg["ts_end"] = ts
        seg["parts"].append((text or "").strip())
        seg["src_ids"].append(rid)
    flush()
    set_bookmark(conn, rows[-1][0])
    conn.commit()
    return len(rows), written, junked


def _word_overlap(a, b):
    """Доля слов исходника, сохранившихся в исправлении (корни по 5 букв)."""
    wa = {w[:5] for w in re.findall(r"[а-яa-z]+", a.lower()) if len(w) > 2}
    wb = {w[:5] for w in re.findall(r"[а-яa-z]+", b.lower()) if len(w) > 2}
    if not wa:
        return 1.0
    return len(wa & wb) / len(wa)


_CORRECT_BACKOFF = [0.0]  # пауза после ошибки сервера коррекции


def correct_pass(conn):
    """Проход LLM-коррекции пользовательских сегментов. Сколько успеет."""
    if time.time() < _CORRECT_BACKOFF[0]:
        return 0
    rows = conn.execute(
        "SELECT id, text FROM segments WHERE corrected IS NULL"
        " AND role = 'user' AND length(text) >= 8"
        " ORDER BY id LIMIT %s", (CORRECT_BATCH,)).fetchall()
    if not rows:
        return 0
    import requests
    n = 0
    for rid, text in rows:
        try:
            r = requests.post(CORRECT_URL, json={
                "model": "corrector",
                "messages": [{"role": "system", "content": CORRECT_SYSTEM},
                             {"role": "user", "content": text}],
                "temperature": 0.0, "max_tokens": 300, "stream": False,
            }, timeout=180)
            r.raise_for_status()
            out = (r.json()["choices"][0]["message"]["content"] or "").strip()
            out = out.strip('"«»')
            if not out:
                corrected = ""          # модель сочла фрагмент шумом
            elif len(out) <= len(text) * 2.5 + 40 and _word_overlap(text, out) >= 0.3:
                corrected = out
            else:
                corrected = text        # страховка: модель выдумала/удалила —
                # оставляем оригинал, чем терять слова
            conn.execute("UPDATE segments SET corrected = %s WHERE id = %s",
                         (corrected, rid))
            n += 1
        except Exception as e:
            print(f"corrector: {e}", flush=True)
            _CORRECT_BACKOFF[0] = time.time() + 60
            break
    conn.commit()
    return n


def embed_pass(conn, emb):
    rows = conn.execute(
        "SELECT id, COALESCE(NULLIF(corrected, ''), text) FROM segments"
        " WHERE embedding IS NULL AND (corrected IS NULL OR corrected <> '')"
        " ORDER BY id LIMIT %s", (BATCH,)).fetchall()
    if not rows:
        return 0
    vecs = list(emb.embed([r[1] for r in rows]))
    for (rid, _), v in zip(rows, vecs):
        conn.execute("UPDATE segments SET embedding = %s WHERE id = %s",
                     (v.tolist(), rid))
    conn.commit()
    return len(rows)


def memory_http_loop():
    """HTTP-эндпоинт семантического поиска для колонки:
    POST /search {"query": "...", "limit": 5} -> релевантные сегменты."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    emb = get_embedder()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/search":
                self.send_error(404)
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                q = (body.get("query") or "").strip()
                limit = min(int(body.get("limit", 5)), 20)
                if not q:
                    self._send({"results": []})
                    return
                vec = list(emb.embed([q]))[0].tolist()
                with psycopg.connect(DSN) as conn:
                    rows = conn.execute(
                        "SELECT to_char(ts_start AT TIME ZONE %s,"
                        " 'YYYY-MM-DD HH24:MI'),"
                        " COALESCE(NULLIF(corrected,''), text),"
                        " embedding <=> %s::vector AS d FROM segments"
                        " WHERE embedding IS NOT NULL"
                        " ORDER BY d LIMIT %s",
                        (MEM_TZ, vec, limit)).fetchall()
                self._send({"results": [
                    {"ts": r[0], "text": r[1], "dist": round(float(r[2]), 3)}
                    for r in rows if float(r[2]) < MEM_MAX_DIST]})
            except Exception as e:
                self._send({"error": str(e)})

        def _send(self, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("0.0.0.0", MEM_PORT), H).serve_forever()


def main():
    emb = get_embedder()
    threading.Thread(target=memory_http_loop, daemon=True).start()
    print(f"memory search: http://0.0.0.0:{MEM_PORT}/search", flush=True)
    while True:
        try:
            with psycopg.connect(DSN, autocommit=False) as conn:
                ensure_schema(conn)
                processed, written, junked = segment_pass(conn)
                if processed:
                    print(f"segmented {processed} -> {written} segments,"
                          f" {junked} junk", flush=True)
                nc = correct_pass(conn)
                if nc:
                    print(f"corrected {nc} segments", flush=True)
                n = embed_pass(conn, emb)
                if n:
                    print(f"embedded {n} segments", flush=True)
                if not processed and not n and not nc:
                    time.sleep(10)
        except Exception as e:
            print(f"pipeline error: {e}", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
