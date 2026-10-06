# -*- coding: utf-8 -*-
"""Слой доступа к SQLite. Одно соединение на поток (Qt-поток и Flask-поток)."""
import os
import json
import sqlite3
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "vocab.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    word         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    ipa          TEXT DEFAULT '',
    translation  TEXT DEFAULT '',
    example_en   TEXT DEFAULT '',
    example_ru   TEXT DEFAULT '',
    level        TEXT DEFAULT '',      -- A1..C2
    tags         TEXT DEFAULT '',      -- через запятую: tense:present-perfect, seasons
    note         TEXT DEFAULT '',
    enriched     INTEGER DEFAULT 0,    -- 0 = ждёт обогащения, 1 = готово
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS srs (
    word_id      INTEGER PRIMARY KEY REFERENCES words(id) ON DELETE CASCADE,
    ease         REAL DEFAULT 2.5,
    interval_min REAL DEFAULT 0,
    due_at       TEXT,
    reps         INTEGER DEFAULT 0,
    lapses       INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'new'    -- new | learning | learned | suspended
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id    INTEGER REFERENCES words(id) ON DELETE CASCADE,
    shown_at   TEXT NOT NULL,
    action     TEXT NOT NULL,          -- know | again | skip | manual
    ms_visible INTEGER DEFAULT 0
);

-- Несколько примеров на слово: карточка показывает их по очереди, поэтому
-- одно и то же слово каждый раз встречается в новом предложении.
CREATE TABLE IF NOT EXISTS sentences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id    INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    text_en    TEXT NOT NULL,
    text_ru    TEXT DEFAULT '',
    source     TEXT DEFAULT 'ai',    -- seed | ai
    shown      INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(word_id, text_en)
);

-- Итоги тренировочных сессий: сколько прошли, что ответили, в каком режиме.
-- Отдельные ответы и так лежат в events, но по ним не видно, где кончилась
-- одна сессия и началась другая.
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    mode        TEXT DEFAULT '',
    deck        TEXT DEFAULT '',
    total       INTEGER DEFAULT 0,
    right_cnt   INTEGER DEFAULT 0,
    wrong_cnt   INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    seconds     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_srs_due    ON srs(due_at);
CREATE INDEX IF NOT EXISTS idx_srs_status ON srs(status);
CREATE INDEX IF NOT EXISTS idx_ev_word    ON events(word_id);
CREATE INDEX IF NOT EXISTS idx_ev_time    ON events(shown_at);
CREATE INDEX IF NOT EXISTS idx_sent_word  ON sentences(word_id);
CREATE INDEX IF NOT EXISTS idx_sess_time  ON sessions(finished_at);
"""

DEFAULTS = {
    "interval_min": "3",            # каждые N минут показывать всплывашку
    "popup_seconds": "14",          # сколько секунд живёт окно
    "hide_translation_ms": "2500",  # задержка перед показом перевода (0 = сразу)
    "quiet_from": "23:00",
    "quiet_to": "08:00",
    "quiet_enabled": "1",
    "paused": "0",
    "pause_until": "",          # пауза до момента (ISO, локальное время)
    "focus_tag": "",                # если задан — 80% показов из этого тега
    "new_per_day": "0",         # 0 = без дневного лимита новых слов
    "know_to_learn": "2",       # сколько раз нажать «Знаю», чтобы слово стало выученным
    # --- цель дня и разбор накопленного ---
    "daily_goal": "5",          # сколько слов в день нужно отметить «Знаю»
    "review_threshold": "25",   # накопилось столько показов без ответа — предложить разбор
    "review_prompt_enabled": "1",
    "review_learned": "0",      # 1 = изредка повторять выученные, 0 = не показывать
    # --- озвучка (по умолчанию выключена: звук в фоне нужен не всем) ---
    "tts_enabled": "0",
    "tts_voice": "uk-female",   # uk-female | uk-male | us-female | us-male
    "card_scale": "auto",     # auto | 0.9 | 1.0 | 1.2 | 1.4 — размер карточки
    "corner": "top-center",   # top-left|top-center|top-right|bottom-left|bottom-center|bottom-right|center
    "margin_px": "28",        # отступ от края экрана
    # --- нейросеть NVIDIA: примеры предложений и пополнение словаря ---
    "ai_enabled": "0",              # 1 = разрешить обращения к нейросети
    "ai_key": "",                   # ключ nvapi-… хранится только здесь, в базе
    "ai_base_url": "https://integrate.api.nvidia.com/v1",
    "ai_model": "deepseek-ai/deepseek-v4.1-flash",
    "ai_sentences_per_word": "4",   # сколько примеров держать на слово
    "ai_min_new_words": "10",       # если новых слов меньше — дозаказать ещё
    "ai_words_per_batch": "20",     # сколько слов просить за раз
    "ai_level": "A2-B1",            # уровень генерируемой лексики
    "ai_grammar": "Present Perfect",  # грамматика, под которую строятся примеры
}


def write(sql, params=()):
    """Запись с повтором: базу могут держать дашборд, фоновый помощник
    или внешний инструмент, и терять из-за этого ответ пользователя нельзя.
    """
    import time as _time
    last = None
    for attempt in range(WRITE_RETRIES):
        try:
            c = conn()
            c.execute(sql, params)
            c.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            last = e
            _time.sleep(0.3 * (attempt + 1))
    raise last


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


BUSY_TIMEOUT = 30      # секунд ждать освобождения базы, прежде чем сдаться
WRITE_RETRIES = 3      # столько раз повторить запись, если база занята


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


# Формы неправильного глагола живут отдельными полями, а не строкой в переводе:
# так их можно разложить таблицей на карточке и спрятать в режиме проверки.
EVENT_COLUMNS = {
    # Откуда пришёл ответ: popup — всплывающая карточка, train — тренировка.
    # Без этого не отделить фоновые показы от осознанного разбора.
    "source": "TEXT DEFAULT 'popup'",
}

EXTRA_COLUMNS = {
    "v2": "TEXT DEFAULT ''",
    "v3": "TEXT DEFAULT ''",
    "ipa2": "TEXT DEFAULT ''",
    "ipa3": "TEXT DEFAULT ''",
    # Русское чтение. Обычно пустое — его считает ruread по транскрипции.
    # Заполняется руками, когда готовый пересчёт не устраивает.
    "ru_read": "TEXT DEFAULT ''",
}


def ensure_columns():
    c = conn()
    have = {r["name"] for r in c.execute("PRAGMA table_info(words)")}
    for name, decl in EXTRA_COLUMNS.items():
        if name not in have:
            c.execute(f"ALTER TABLE words ADD COLUMN {name} {decl}")
    have_ev = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
    for name, decl in EVENT_COLUMNS.items():
        if name not in have_ev:
            c.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
    c.commit()


def init():
    c = conn()
    c.executescript(SCHEMA)
    ensure_columns()
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    # Страховка от ручной правки базы: слово без строки в srs не попадёт
    # ни в выборку показа, ни в фильтры дашборда.
    c.execute("""INSERT INTO srs(word_id, due_at, status)
                 SELECT w.id, ?, 'new' FROM words w
                 LEFT JOIN srs s ON s.word_id = w.id
                 WHERE s.word_id IS NULL""", (now_iso(),))
    c.commit()


def get(key, default=None):
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key, default)
    return row["value"]


def get_int(key, default=0):
    try:
        return int(float(get(key, default)))
    except (TypeError, ValueError):
        return default


def put(key, value):
    c = conn()
    c.execute(
        "INSERT INTO settings(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    c.commit()


def all_settings():
    return {r["key"]: r["value"] for r in conn().execute("SELECT key, value FROM settings")}


def merge_tags(old, new):
    """Объединяет теги без дублей, сохраняя порядок: слово может жить в нескольких колодах."""
    seen, out = set(), []
    for t in [x.strip() for x in f"{old or ''},{new or ''}".split(",")]:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return ", ".join(out)


def add_word(word, ipa="", translation="", example_en="", example_ru="",
             level="", tags="", note="", enriched=None, overwrite=False,
             ru_read=""):
    """Добавляет слово.

    По умолчанию существующее слово только дополняется — пустые поля заполняются,
    заполненные остаются как есть. С overwrite=True переданные непустые значения
    заменяют старые: нужно, когда колода приносит более полную карточку
    (например, три формы глагола вместо одного перевода).
    """
    word = (word or "").strip()
    if not word:
        return None, "empty"
    if enriched is None:
        enriched = 1 if (translation and ipa) else 0
    c = conn()
    row = c.execute("SELECT id FROM words WHERE word=? COLLATE NOCASE", (word,)).fetchone()
    if row and overwrite:
        wid = row["id"]
        cur_tags = c.execute("SELECT tags FROM words WHERE id=?", (wid,)).fetchone()["tags"]
        sets, params = [], []
        for field, value in (("ipa", ipa), ("translation", translation),
                             ("example_en", example_en), ("example_ru", example_ru),
                             ("level", level), ("ru_read", ru_read)):
            if value:
                sets.append(f"{field}=?")
                params.append(value)
        sets.append("tags=?")
        params.append(merge_tags(cur_tags, tags))
        params.append(wid)
        c.execute(f"UPDATE words SET {', '.join(sets)} WHERE id=?", params)
        c.execute("UPDATE words SET enriched=1 WHERE id=? AND ipa!='' AND translation!=''", (wid,))
        c.commit()
        return wid, "updated"
    if row:
        wid = row["id"]
        c.execute("""
            UPDATE words SET
                ipa         = CASE WHEN ipa=''         THEN ? ELSE ipa         END,
                translation = CASE WHEN translation='' THEN ? ELSE translation END,
                example_en  = CASE WHEN example_en=''  THEN ? ELSE example_en  END,
                example_ru  = CASE WHEN example_ru=''  THEN ? ELSE example_ru  END,
                level       = CASE WHEN level=''       THEN ? ELSE level       END,
                ru_read     = CASE WHEN ru_read=''     THEN ? ELSE ru_read     END,
                tags        = CASE WHEN tags=''        THEN ? ELSE tags        END
            WHERE id=?""",
            (ipa, translation, example_en, example_ru, level, ru_read, tags, wid))
        c.execute("UPDATE words SET enriched=1 WHERE id=? AND ipa!='' AND translation!=''", (wid,))
        c.commit()
        return wid, "updated"
    cur = c.execute("""
        INSERT INTO words(word, ipa, translation, example_en, example_ru,
                          level, tags, note, enriched, created_at, ru_read)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (word, ipa, translation, example_en, example_ru, level, tags, note,
         int(enriched), now_iso(), ru_read))
    wid = cur.lastrowid
    c.execute("INSERT INTO srs(word_id, due_at, status) VALUES (?, ?, 'new')", (wid, now_iso()))
    c.commit()
    return wid, "created"


def add_sentence(word_id, text_en, text_ru="", source="ai"):
    """Добавляет пример. Дубли по тексту игнорируются."""
    text_en = (text_en or "").strip()
    if not text_en:
        return False
    c = conn()
    try:
        c.execute("""INSERT INTO sentences(word_id, text_en, text_ru, source, created_at)
                     VALUES (?,?,?,?,?)""",
                  (word_id, text_en, (text_ru or "").strip(), source, now_iso()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def pick_sentence(word_id):
    """Наименее показанный пример — так примеры идут по кругу, а не один и тот же."""
    return conn().execute(
        """SELECT * FROM sentences WHERE word_id=?
           ORDER BY shown, RANDOM() LIMIT 1""", (word_id,)).fetchone()


def mark_sentence_shown(sentence_id):
    write("UPDATE sentences SET shown = shown + 1 WHERE id=?", (sentence_id,))


def sentence_count(word_id):
    return conn().execute("SELECT COUNT(*) FROM sentences WHERE word_id=?", (word_id,)).fetchone()[0]


def set_forms(word_id, v2="", v3="", ipa2="", ipa3=""):
    c = conn()
    c.execute("UPDATE words SET v2=?, v3=?, ipa2=?, ipa3=? WHERE id=?",
              (v2, v3, ipa2, ipa3, word_id))
    c.commit()


def log_event(word_id, action, ms_visible=0, source="popup"):
    write("""INSERT INTO events(word_id, shown_at, action, ms_visible, source)
             VALUES (?,?,?,?,?)""",
          (word_id, now_iso(), action, int(ms_visible), source))


def log_session(mode, deck, total, right_cnt, wrong_cnt, skipped, seconds, started_at=None):
    """Итог тренировки. Пишется один раз, когда сессия дошла до конца."""
    c = conn()
    c.execute("""INSERT INTO sessions(started_at, finished_at, mode, deck,
                                      total, right_cnt, wrong_cnt, skipped, seconds)
                 VALUES (?,?,?,?,?,?,?,?,?)""",
              (started_at or now_iso(), now_iso(), mode or "", deck or "",
               int(total), int(right_cnt), int(wrong_cnt), int(skipped), int(seconds)))
    c.commit()
    return c.execute("SELECT last_insert_rowid()").fetchone()[0]
