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

-- Удалённые слова. Без этого списка удаление не доехало бы до сервера:
-- синхронизация видела бы только то, что есть, и вернула бы слово обратно.
CREATE TABLE IF NOT EXISTS deletions (
    word       TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);

-- Докуда дошла синхронизация. Хранится локально: у каждой машины свой счёт.
CREATE TABLE IF NOT EXISTS sync_state (
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
    "ai_model": "nvidia/nemotron-3-super-120b-a12b",
    "ai_sentences_per_word": "4",   # сколько примеров держать на слово
    "ai_min_new_words": "10",       # если новых слов меньше — дозаказать ещё

    # --- резервные копии ---
    # Копия раз в сутки и недельный запас: словарь с прогрессом живёт
    # в одном файле, и терять его из-за сбоя диска было бы обидно.
    "backup_enabled": "1",
    "backup_keep": "7",            # сколько копий держать
    "backup_at": "",               # когда снимали последнюю

    # --- синхронизация с сервером ---
    # Словарь и прогресс живут локально и уезжают на сервер копией: так
    # карточки показываются и без сети, а данные хранятся не в одном месте.
    # Пароль и путь к ключу лежат здесь же, в базе, — как ключ нейросети.
    "sync_enabled": "0",
    "sync_host": "127.0.0.1",
    "sync_port": "13306",
    "sync_db": "",
    "sync_user": "",
    "sync_password": "",
    "sync_ssh_host": "",
    "sync_ssh_user": "",
    "sync_ssh_key": "",
    "sync_every_min": "15",        # как часто сверяться, когда есть связь
    "sync_at": "",                 # когда сверялись в последний раз
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

# Когда слово перешло в «выучено». По событиям это не восстановить задним
# числом точно: порог «сколько верных ответов» мог меняться, да и сам момент
# перехода в events не записан — там лежат ответы, а не смены статуса.
SRS_COLUMNS = {
    "learned_at": "TEXT DEFAULT ''",
    "first_shown_at": "TEXT DEFAULT ''",
    # Слово, которое пользователь руками вернул в изучение. Обычная очередь
    # показала бы его часов через шесть: свежеотвеченное блокируется списком
    # недавних, а дальше на один созревший повтор приходится меньше двух
    # процентов показов. Явное действие должно срабатывать заметно быстрее.
    "priority_at": "TEXT DEFAULT ''",
}

# Отметка последней правки. По ней синхронизация решает, чья версия строки
# свежее: сравнивать содержимое бессмысленно, а порядковый номер у каждой
# машины свой.
SYNC_WORD_COLUMNS = {"updated_at": "TEXT DEFAULT ''"}
# Журнал показов и сессии только дописываются, но их номера на разных машинах
# совпадут и перепутаются. Поэтому у каждой строки свой случайный ключ.
UID_COLUMN = {"uid": "TEXT DEFAULT ''"}

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
    have_srs = {r["name"] for r in c.execute("PRAGMA table_info(srs)")}
    added_srs = [n for n in SRS_COLUMNS if n not in have_srs]
    for name in added_srs:
        c.execute(f"ALTER TABLE srs ADD COLUMN {name} {SRS_COLUMNS[name]}")

    # --- колонки для синхронизации ---
    for table, columns in (("words", SYNC_WORD_COLUMNS), ("srs", SYNC_WORD_COLUMNS),
                           ("events", UID_COLUMN), ("sessions", UID_COLUMN)):
        present = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in present:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    c.commit()
    ensure_sync_triggers()
    backfill_sync_keys()
    if added_srs:
        backfill_history()


# Отметку правки и случайный ключ проставляют триггеры, а не код приложения.
# Записей в words и srs два десятка по всему проекту, и любая забытая строчка
# означала бы, что изменение тихо не уедет на сервер.
SYNC_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS words_touch_ins AFTER INSERT ON words
WHEN NEW.updated_at = '' OR NEW.updated_at IS NULL
BEGIN UPDATE words SET updated_at = (strftime('%Y-%m-%dT%H:%M:%S','now') || '+00:00')
      WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS words_touch_upd AFTER UPDATE ON words
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE words SET updated_at = (strftime('%Y-%m-%dT%H:%M:%S','now') || '+00:00')
      WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS srs_touch_ins AFTER INSERT ON srs
WHEN NEW.updated_at = '' OR NEW.updated_at IS NULL
BEGIN UPDATE srs SET updated_at = (strftime('%Y-%m-%dT%H:%M:%S','now') || '+00:00')
      WHERE word_id = NEW.word_id; END;

CREATE TRIGGER IF NOT EXISTS srs_touch_upd AFTER UPDATE ON srs
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE srs SET updated_at = (strftime('%Y-%m-%dT%H:%M:%S','now') || '+00:00')
      WHERE word_id = NEW.word_id; END;

CREATE TRIGGER IF NOT EXISTS events_uid AFTER INSERT ON events
WHEN NEW.uid = '' OR NEW.uid IS NULL
BEGIN UPDATE events SET uid = lower(hex(randomblob(16))) WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS sessions_uid AFTER INSERT ON sessions
WHEN NEW.uid = '' OR NEW.uid IS NULL
BEGIN UPDATE sessions SET uid = lower(hex(randomblob(16))) WHERE id = NEW.id; END;

-- Удаление слова оставляет след, иначе синхронизация вернёт его с сервера
CREATE TRIGGER IF NOT EXISTS words_tombstone AFTER DELETE ON words
BEGIN INSERT OR REPLACE INTO deletions(word, deleted_at)
      VALUES (lower(OLD.word), (strftime('%Y-%m-%dT%H:%M:%S','now') || '+00:00')); END;
"""


TRIGGER_NAMES = ("words_touch_ins", "words_touch_upd", "srs_touch_ins",
                 "srs_touch_upd", "events_uid", "sessions_uid", "words_tombstone")


def ensure_sync_triggers():
    """Пересоздаёт триггеры целиком.

    `CREATE TRIGGER IF NOT EXISTS` оставил бы старое тело в базах, которые
    уже обновлялись, — а тело менялось вместе с форматом времени.
    """
    c = conn()
    for name in TRIGGER_NAMES:
        c.execute(f"DROP TRIGGER IF EXISTS {name}")
    c.executescript(SYNC_TRIGGERS)
    c.commit()


def backfill_sync_keys():
    """Проставляет ключи строкам, появившимся до синхронизации.

    Без этого вся прежняя история осталась бы без опознавательных знаков
    и уехала бы на сервер дубликатами при каждом запуске.
    """
    c = conn()
    c.execute("UPDATE words SET updated_at = created_at WHERE updated_at = '' OR updated_at IS NULL")
    c.execute("""UPDATE srs SET updated_at = COALESCE(NULLIF(learned_at,''),
                                                      NULLIF(due_at,''), '1970-01-01T00:00:00+00:00')
                 WHERE updated_at = '' OR updated_at IS NULL""")
    for table in ("events", "sessions"):
        c.execute(f"""UPDATE {table} SET uid = lower(hex(randomblob(16)))
                      WHERE uid = '' OR uid IS NULL""")
    c.commit()


def backfill_history():
    """Восстанавливает по событиям то, что раньше не записывалось.

    Первый показ виден в events прямо. Момент «выучено» — приблизительно:
    берём ответ «знаю», после которого их накопилось столько, сколько сейчас
    требует настройка. Для слов, выученных при другом пороге, дата сдвинется,
    но это честнее пустой колонки: иначе весь прошлый прогресс останется
    без дат и не попадёт ни в один отчёт.
    """
    c = conn()
    c.execute("""UPDATE srs SET first_shown_at = COALESCE(
                     (SELECT MIN(e.shown_at) FROM events e WHERE e.word_id = srs.word_id), '')
                 WHERE first_shown_at = ''""")
    need = max(1, get_int("know_to_learn", 2))
    # Трёхступенчатый запас: нужный по счёту «знаю»; если событий меньше,
    # чем повторов (в первые дни писались не все ответы) — последний «знаю»;
    # если и его нет — последний показ вообще.
    c.execute(f"""UPDATE srs SET learned_at = COALESCE(
                     (SELECT e.shown_at FROM events e
                      WHERE e.word_id = srs.word_id AND e.action = 'know'
                      ORDER BY e.shown_at LIMIT 1 OFFSET {need - 1}),
                     (SELECT MAX(e.shown_at) FROM events e
                      WHERE e.word_id = srs.word_id AND e.action = 'know'),
                     (SELECT MAX(e.shown_at) FROM events e
                      WHERE e.word_id = srs.word_id),
                     '')
                  WHERE status = 'learned' AND learned_at = ''""")
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
    # Дата первой встречи со словом. Нужна, чтобы считать, сколько дней оно
    # в работе: по одному только created_at этого не видно — слово может
    # пролежать в словаре месяц, ни разу не показавшись.
    write("UPDATE srs SET first_shown_at=? WHERE word_id=? AND first_shown_at=''",
          (now_iso(), word_id))


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
