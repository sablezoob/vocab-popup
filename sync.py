# -*- coding: utf-8 -*-
"""Синхронизация словаря и прогресса с удалённой базой.

Устройство. Рабочая база остаётся локальной — карточки должны показываться
и без сети. Сервер хранит копию и служит точкой правды между машинами:
то, что сделано здесь, уезжает туда, то, что сделано на другой машине,
приезжает сюда.

Как сверяются строки. Номера строк у каждой машины свои и совпадут
случайно, поэтому ключом служит само слово, а у журнала показов и сессий —
случайный uid, выданный при вставке. Спор решается отметкой `updated_at`:
побеждает та версия, которую правили позже. Для журнала спорить не о чем —
он только дописывается, и строки просто объединяются.

Удаление слова оставляет запись в `deletions`: без неё сервер вернул бы
слово обратно при следующей же сверке.
"""
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

import db
import remote

log = logging.getLogger("vocab.sync")

BATCH = 500
# Запас на расхождение часов между машинами.
SKEW_SECONDS = 3600
EPOCH = "1970-01-01T00:00:00+00:00"

WORD_COLS = ["ipa", "translation", "example_en", "example_ru", "level",
             "tags", "note", "enriched", "created_at", "v2", "v3",
             "ipa2", "ipa3", "ru_read", "updated_at"]
SRS_COLS = ["ease", "interval_min", "due_at", "reps", "lapses", "status",
            "learned_at", "first_shown_at", "priority_at", "updated_at"]
EVENT_COLS = ["uid", "word", "shown_at", "action", "ms_visible", "source"]
SENT_COLS = ["k", "word", "text_en", "text_ru", "source", "shown", "created_at"]
SESSION_COLS = ["uid", "started_at", "finished_at", "mode", "deck", "total",
                "right_cnt", "wrong_cnt", "skipped", "seconds"]

# Настройки, которые относятся к учёбе и одинаковы на всех машинах.
# Всё остальное — положение окна, паузы, ключи, адреса — живёт на своей
# машине: синхронизировать их значило бы тянуть чужие окна и чужие паузы.
SHARED_SETTINGS = [
    "interval_min", "popup_seconds", "hide_translation_ms", "quiet_from",
    "quiet_to", "focus_tag", "new_per_day", "know_to_learn", "review_learned",
    "daily_goal", "review_threshold", "ai_sentences_per_word",
    "ai_min_new_words", "tts_voice",
]


# ---------- отметки «докуда дошли» ----------

def mark(key, value=None):
    c = db.conn()
    if value is None:
        row = c.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""
    c.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?,?)", (key, value))
    c.commit()
    return value


def _since_value(key):
    """Отметка, сдвинутая назад на запас по часам.

    Повторно отправленная строка ничего не ломает — она отсеется по ключу,
    а потерянная пропадёт навсегда, поэтому запас берём в свою сторону.
    """
    was = (mark(key) or "").strip()
    if not was:
        return EPOCH
    try:
        at = datetime.fromisoformat(was)
    except ValueError:
        return EPOCH
    return (at - timedelta(seconds=SKEW_SECONDS)).isoformat(timespec="seconds")


def advance(key, newest):
    """Двигает отметку «докуда дошли», но не дальше текущего момента.

    Одна строка с датой из будущего — сбитые часы на другой машине, правка
    руками в DBeaver — иначе уносила бы отметку туда же, и обмен вставал бы
    навсегда: всё последующее оказывалось бы «старее уже полученного».
    Строки из будущего будут приезжать повторно, но это дёшево и безвредно.
    """
    now = db.now_iso()
    if not newest:
        return
    value = min(str(newest), now)
    if value > (mark(key) or ""):
        mark(key, value)


def sent_key(word, text_en):
    """Ключ предложения: пара «слово + текст» уникальна и без номеров."""
    raw = f"{(word or '').lower()}|{(text_en or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------- выгрузка на сервер ----------

def _push_words(lc, rc):
    rows = lc.execute(
        f"""SELECT w.word, {', '.join('w.' + x for x in WORD_COLS)}
            FROM words w WHERE w.updated_at > ?""", (_since_value("words_push"),)).fetchall()
    if not rows:
        return 0
    cols = ["word"] + WORD_COLS
    marks = ",".join(["%s"] * len(cols))
    # Поле обновляется, только если пришедшая версия свежее здешней.
    upd = ", ".join(
        f"{c} = IF(new.updated_at > words.updated_at, new.{c}, words.{c})"
        for c in WORD_COLS if c != "updated_at")
    sql = (f"INSERT INTO words ({', '.join(cols)}) VALUES ({marks}) AS new "
           f"ON DUPLICATE KEY UPDATE {upd}, "
           f"updated_at = GREATEST(words.updated_at, new.updated_at)")
    data = [tuple(r[c] for c in cols) for r in rows]
    with rc.cursor() as cur:
        for i in range(0, len(data), BATCH):
            cur.executemany(sql, data[i:i + BATCH])
    return len(data)


def _push_srs(lc, rc):
    rows = lc.execute(
        f"""SELECT w.word, {', '.join('s.' + x for x in SRS_COLS)}
            FROM srs s JOIN words w ON w.id = s.word_id
            WHERE s.updated_at > ?""", (_since_value("srs_push"),)).fetchall()
    if not rows:
        return 0
    cols = ["word"] + SRS_COLS
    marks = ",".join(["%s"] * len(cols))
    upd = ", ".join(f"{c} = IF(new.updated_at > srs.updated_at, new.{c}, srs.{c})"
                    for c in SRS_COLS if c != "updated_at")
    sql = (f"INSERT INTO srs ({', '.join(cols)}) VALUES ({marks}) AS new "
           f"ON DUPLICATE KEY UPDATE {upd}, "
           f"updated_at = GREATEST(srs.updated_at, new.updated_at)")
    data = [tuple(r[c] for c in cols) for r in rows]
    with rc.cursor() as cur:
        for i in range(0, len(data), BATCH):
            cur.executemany(sql, data[i:i + BATCH])
    return len(data)


def _push_events(lc, rc):
    rows = lc.execute(
        """SELECT e.uid, w.word, e.shown_at, e.action, e.ms_visible, e.source
           FROM events e JOIN words w ON w.id = e.word_id
           WHERE e.shown_at > ?""", (_since_value("events_push"),)).fetchall()
    if not rows:
        return 0
    sql = ("INSERT IGNORE INTO events (uid, word, shown_at, action, ms_visible, source) "
           "VALUES (%s,%s,%s,%s,%s,%s)")
    data = [tuple(r[c] for c in EVENT_COLS) for r in rows]
    with rc.cursor() as cur:
        for i in range(0, len(data), BATCH):
            cur.executemany(sql, data[i:i + BATCH])
    return len(data)


def _push_sentences(lc, rc):
    rows = lc.execute(
        """SELECT w.word, s.text_en, s.text_ru, s.source, s.shown, s.created_at
           FROM sentences s JOIN words w ON w.id = s.word_id
           WHERE s.created_at > ?""", (_since_value("sent_push"),)).fetchall()
    if not rows:
        return 0
    sql = ("INSERT IGNORE INTO sentences (k, word, text_en, text_ru, source, shown, created_at) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s)")
    data = [(sent_key(r["word"], r["text_en"]), r["word"], r["text_en"],
             r["text_ru"], r["source"], r["shown"], r["created_at"]) for r in rows]
    with rc.cursor() as cur:
        for i in range(0, len(data), BATCH):
            cur.executemany(sql, data[i:i + BATCH])
    return len(data)


def _push_sessions(lc, rc):
    rows = lc.execute("SELECT * FROM sessions WHERE finished_at > ?",
                      (_since_value("sessions_push"),)).fetchall()
    if not rows:
        return 0
    marks = ",".join(["%s"] * len(SESSION_COLS))
    sql = f"INSERT IGNORE INTO sessions ({', '.join(SESSION_COLS)}) VALUES ({marks})"
    data = [tuple(r[c] for c in SESSION_COLS) for r in rows]
    with rc.cursor() as cur:
        cur.executemany(sql, data)
    return len(data)


def _push_deletions(lc, rc):
    rows = lc.execute("SELECT word, deleted_at FROM deletions").fetchall()
    if not rows:
        return 0
    with rc.cursor() as cur:
        cur.executemany(
            "INSERT INTO deletions (word, deleted_at) VALUES (%s,%s) "
            "ON DUPLICATE KEY UPDATE deleted_at = GREATEST(deletions.deleted_at, VALUES(deleted_at))",
            [(r["word"], r["deleted_at"]) for r in rows])
        # Со стороны сервера слово тоже должно исчезнуть, иначе оно вернётся
        for r in rows:
            cur.execute("DELETE FROM words WHERE word = %s", (r["word"],))
            cur.execute("DELETE FROM srs WHERE word = %s", (r["word"],))
    return len(rows)


def _push_settings(lc, rc):
    """Настройки уезжают на сервер, но обратно не приезжают.

    Это не прогресс, а наладка под конкретную машину. Хранить их полезно —
    видно, с какими порогами шла учёба, — а тянуть обратно значило бы
    переписывать здешние настройки чужими при каждой сверке.
    """
    marks = [(k, db.get(k, "")) for k in SHARED_SETTINGS]
    now = db.now_iso()
    with rc.cursor() as cur:
        cur.executemany(
            "INSERT INTO settings (k, value, updated_at) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at)",
            [(k, v, now) for k, v in marks])
    return len(marks)


# ---------- загрузка с сервера ----------

def _word_id(lc, word, created_at, updated_at):
    """Находит слово по тексту, при надобности заводит заготовку.

    Заготовка без перевода на экран не попадёт — такую проверку делает
    выборка показа, — поэтому порядок прихода строк не важен.
    """
    row = lc.execute("SELECT id FROM words WHERE word = ? COLLATE NOCASE", (word,)).fetchone()
    if row:
        return row["id"]
    cur = lc.execute(
        "INSERT INTO words(word, created_at, updated_at) VALUES (?,?,?)",
        (word, created_at or db.now_iso(), updated_at or db.now_iso()))
    return cur.lastrowid


def _ensure_srs_rows(lc):
    """Заводит расписание словам, приехавшим с сервера.

    Без строки в srs слово не попадает ни в выборку показа, ни в фильтры
    дашборда: оно лежит в словаре и молчит. Та же страховка есть при запуске
    приложения, но ждать перезапуска после каждой сверки незачем.
    """
    n = lc.execute("""INSERT INTO srs(word_id, due_at, status)
                      SELECT w.id, ?, 'new' FROM words w
                      LEFT JOIN srs s ON s.word_id = w.id
                      WHERE s.word_id IS NULL""", (db.now_iso(),)).rowcount
    lc.commit()
    return max(0, n)


def _pull_deletions(lc, rc):
    since = mark("del_pull") or EPOCH
    with rc.cursor() as cur:
        cur.execute("SELECT word, deleted_at FROM deletions WHERE deleted_at > %s", (since,))
        rows = cur.fetchall()
    n = 0
    for r in rows:
        # Слово, изменённое здесь уже после удаления, не трогаем: правка
        # свежее — значит, им снова пользуются
        got = lc.execute("SELECT id, updated_at FROM words WHERE word = ? COLLATE NOCASE",
                         (r["word"],)).fetchone()
        if got and (got["updated_at"] or "") <= r["deleted_at"]:
            lc.execute("DELETE FROM words WHERE id=?", (got["id"],))
            n += 1
    lc.commit()
    if rows:
        advance("del_pull", max(r["deleted_at"] for r in rows))
    return n


def _pull_words(lc, rc):
    since = mark("words_pull") or EPOCH
    with rc.cursor() as cur:
        cur.execute(f"SELECT word, {', '.join(WORD_COLS)} FROM words WHERE updated_at > %s",
                    (since,))
        rows = cur.fetchall()
    n = 0
    sets = ", ".join(f"{c}=excluded.{c}" for c in WORD_COLS)
    for r in rows:
        cols = ["word"] + WORD_COLS
        lc.execute(
            f"""INSERT INTO words ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})
                ON CONFLICT(word) DO UPDATE SET {sets}
                WHERE excluded.updated_at > words.updated_at""",
            tuple(r[c] for c in cols))
        n += 1
    lc.commit()
    if rows:
        advance("words_pull", max(r["updated_at"] for r in rows))
    return n


def _pull_srs(lc, rc):
    since = mark("srs_pull") or EPOCH
    with rc.cursor() as cur:
        cur.execute(f"SELECT word, {', '.join(SRS_COLS)} FROM srs WHERE updated_at > %s",
                    (since,))
        rows = cur.fetchall()
    n = 0
    sets = ", ".join(f"{c}=excluded.{c}" for c in SRS_COLS)
    for r in rows:
        wid = _word_id(lc, r["word"], None, r["updated_at"])
        cols = ["word_id"] + SRS_COLS
        lc.execute(
            f"""INSERT INTO srs ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})
                ON CONFLICT(word_id) DO UPDATE SET {sets}
                WHERE excluded.updated_at > srs.updated_at""",
            tuple([wid] + [r[c] for c in SRS_COLS]))
        n += 1
    lc.commit()
    if rows:
        advance("srs_pull", max(r["updated_at"] for r in rows))
    return n


def _pull_events(lc, rc):
    since = mark("events_pull") or EPOCH
    with rc.cursor() as cur:
        cur.execute("""SELECT uid, word, shown_at, action, ms_visible, source
                       FROM events WHERE shown_at > %s ORDER BY shown_at""", (since,))
        rows = cur.fetchall()
    have = {r["uid"] for r in lc.execute(
        "SELECT uid FROM events WHERE shown_at > ?", (since,))}
    n = 0
    for r in rows:
        if r["uid"] in have:
            continue
        wid = _word_id(lc, r["word"], r["shown_at"], r["shown_at"])
        lc.execute("""INSERT INTO events(word_id, shown_at, action, ms_visible, source, uid)
                      VALUES (?,?,?,?,?,?)""",
                   (wid, r["shown_at"], r["action"], r["ms_visible"], r["source"], r["uid"]))
        n += 1
    lc.commit()
    if rows:
        advance("events_pull", max(r["shown_at"] for r in rows))
    return n


def _pull_sentences(lc, rc):
    since = mark("sent_pull") or EPOCH
    with rc.cursor() as cur:
        cur.execute("""SELECT word, text_en, text_ru, source, shown, created_at
                       FROM sentences WHERE created_at > %s""", (since,))
        rows = cur.fetchall()
    n = 0
    for r in rows:
        wid = _word_id(lc, r["word"], r["created_at"], r["created_at"])
        try:
            lc.execute("""INSERT OR IGNORE INTO sentences
                          (word_id, text_en, text_ru, source, created_at, shown)
                          VALUES (?,?,?,?,?,?)""",
                       (wid, r["text_en"], r["text_ru"], r["source"],
                        r["created_at"], r["shown"]))
            n += 1
        except Exception:
            log.exception("Предложение не встало: %s", r["text_en"][:50])
    lc.commit()
    if rows:
        advance("sent_pull", max(r["created_at"] for r in rows))
    return n


def _pull_sessions(lc, rc):
    since = mark("sessions_pull") or EPOCH
    with rc.cursor() as cur:
        cur.execute(f"SELECT {', '.join(SESSION_COLS)} FROM sessions WHERE finished_at > %s",
                    (since,))
        rows = cur.fetchall()
    have = {r["uid"] for r in lc.execute("SELECT uid FROM sessions")}
    n = 0
    for r in rows:
        if r["uid"] in have:
            continue
        cols = SESSION_COLS
        lc.execute(f"""INSERT INTO sessions ({', '.join(cols)})
                       VALUES ({','.join('?' * len(cols))})""",
                   tuple(r[c] for c in cols))
        n += 1
    lc.commit()
    if rows:
        advance("sessions_pull", max(r["finished_at"] for r in rows))
    return n


# ---------- всё вместе ----------

def run():
    """Полная сверка. Возвращает, сколько строк прошло в обе стороны."""
    started = time.time()
    lc = db.conn()
    rc = remote.connect()
    remote.ensure_schema(rc)

    out = {"pushed": {}, "pulled": {}}
    try:
        # Сначала удаления: иначе слово успеет уехать туда-обратно
        out["pushed"]["deletions"] = _push_deletions(lc, rc)
        out["pushed"]["words"] = _push_words(lc, rc)
        out["pushed"]["srs"] = _push_srs(lc, rc)
        out["pushed"]["events"] = _push_events(lc, rc)
        out["pushed"]["sentences"] = _push_sentences(lc, rc)
        out["pushed"]["sessions"] = _push_sessions(lc, rc)
        out["pushed"]["settings"] = _push_settings(lc, rc)
        rc.commit()
    except Exception:
        rc.rollback()
        raise

    now = db.now_iso()
    for key in ("words_push", "srs_push", "events_push", "sent_push", "sessions_push"):
        mark(key, now)
    # Отправленное своё обратно тянуть незачем
    for key in ("words_pull", "srs_pull", "events_pull", "sent_pull", "sessions_pull"):
        if not mark(key):
            mark(key, "1970-01-01T00:00:00+00:00")

    out["pulled"]["deletions"] = _pull_deletions(lc, rc)
    out["pulled"]["words"] = _pull_words(lc, rc)
    out["pulled"]["srs"] = _pull_srs(lc, rc)
    out["pulled"]["events"] = _pull_events(lc, rc)
    out["pulled"]["sentences"] = _pull_sentences(lc, rc)
    out["pulled"]["sessions"] = _pull_sessions(lc, rc)
    out["pulled"]["scheduled"] = _ensure_srs_rows(lc)

    # Своё удаление доехало — список можно очистить
    lc.execute("DELETE FROM deletions")
    lc.commit()

    db.put("sync_at", now)
    out["seconds"] = round(time.time() - started, 1)
    log.info("Синхронизация: отправлено %s, получено %s за %s с",
             out["pushed"], out["pulled"], out["seconds"])
    return out


def due():
    """Пора ли сверяться."""
    if db.get("sync_enabled", "0") != "1" or not remote.is_configured():
        return False
    last = (db.get("sync_at", "") or "").strip()
    if not last:
        return True
    try:
        was = datetime.fromisoformat(last)
    except ValueError:
        return True
    every = max(1, db.get_int("sync_every_min", 15))
    return (datetime.now(timezone.utc) - was).total_seconds() >= every * 60


def tick():
    """Вызывается планировщиком: сверяется, если подошёл срок и есть связь."""
    if not due():
        return None
    return run()


def status():
    """Что показать в дашборде."""
    st = {"enabled": db.get("sync_enabled", "0") == "1",
          "configured": remote.is_configured(),
          "every_min": db.get_int("sync_every_min", 15),
          "last_at": db.get("sync_at", "") or "",
          "tunnel": remote.port_open(),
          "pending": {}}
    lc = db.conn()
    one = lambda sql, p=(): lc.execute(sql, p).fetchone()[0]
    st["pending"] = {
        "words": one("SELECT COUNT(*) FROM words WHERE updated_at > ?",
                     (_since_value("words_push"),)),
        "events": one("SELECT COUNT(*) FROM events WHERE shown_at > ?",
                      (_since_value("events_push"),)),
        "deletions": one("SELECT COUNT(*) FROM deletions"),
    }
    return st
