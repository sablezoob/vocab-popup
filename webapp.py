# -*- coding: utf-8 -*-
"""Локальный дашборд на 127.0.0.1:8777 — импорт слов, редактирование, статистика."""
import logging
import re
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

import ai
import aiworker
import db
import quiz
import ruread
import srs
import theme

app = Flask(__name__)

# Разделители намеренно узкие: тире не годится — оно живёт внутри примеров.
SPLIT_RE = re.compile(r"\s*(?:\||	|;;)\s*")
# Запись из конспекта: «Always- всегда (олвэйз)». Тире тут разделитель, но
# только когда справа кириллица, — иначе под раздачу попали бы catch-up и co-op.
DASH_RE = re.compile(r"^([A-Za-z][A-Za-z'’ .]*?)\s*[-–—:]\s*(?=.*[а-яёА-ЯЁ])(.+)$")
# Та же запись, но без тире: «mindless бездумный (майндлэс)». Латиница слева,
# кириллица справа — границу видно и без знака.
GAP_RE = re.compile(r"^([A-Za-z][A-Za-z'’ .]*?)\s+(?=[а-яёА-ЯЁ])(.+)$")
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
# Чтение русскими буквами в скобках, в конце перевода
RU_READ_RE = re.compile(r"[\(\[]\s*([^()\[\]]*[а-яёА-ЯЁ][^()\[\]]*?)\s*[\)\]]\s*$")


def _looks_like_reading(text, word):
    """Отличает чтение от пояснения в тех же скобках.

    «would — бы (в сокращённой форме пишется как I'd)» — это пояснение,
    а «always — всегда (олвэйз)» — чтение. Разница в размере: чтение идёт
    слог в слог со словом, пояснение всегда длиннее и многословнее.
    """
    text = (text or "").strip()
    if not text or "," in text or re.search(r"[A-Za-z]", text):
        return False
    return (len(text.split()) <= len(word.split())
            and len(text) <= 2.5 * len(word) + 4)


def _with_reading(row):
    """Строка словаря для веба: пустая колонка ru_read заменяется пересчётом.

    В базе чтение хранится только там, где его записали руками, — считать
    его на каждом слове заранее значило бы держать копию, которая устареет
    при первой же правке правил пересчёта.
    """
    d = dict(row)
    d["ru_read"] = ruread.show(row)
    for n in ("2", "3"):
        if d.get("v" + n):
            d["ru_read" + n] = ruread.from_ipa(d.get("ipa" + n), d["v" + n])
    return d


def parse_line(line):
    """Разбирает строку импорта.

    Поддерживает: `word`, `word | перевод`, `word | перевод | /ipa/`,
    `word | перевод | /ipa/ | example en | пример ру`.
    Разделитель — вертикальная черта, таб или `;;`.

    Отдельно понимает запись из конспекта — `always - всегда (олвэйз)`, —
    чтобы список с урока можно было вставить в дашборд как есть, не
    переписывая под формат. Скобки с кириллицей внутри любой части
    читаются как русское чтение.
    """
    line = line.strip().lstrip("-–—•*·").strip()
    if not line:
        return None
    parts = [p.strip() for p in SPLIT_RE.split(line) if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        m = DASH_RE.match(parts[0]) or GAP_RE.match(parts[0])
        if m:
            parts = [m.group(1).strip(), m.group(2).strip()]
    out = {"word": parts[0], "translation": "", "ipa": "", "ru_read": "",
           "example_en": "", "example_ru": ""}
    rest = parts[1:]
    # IPA может стоять в любой позиции — узнаём по слешам или квадратным
    # скобкам. Кириллица внутри скобок — не IPA, а русское чтение.
    for p in list(rest):
        wrapped = ((p.startswith("/") and p.endswith("/"))
                   or (p.startswith("[") and p.endswith("]")))
        if wrapped and not CYRILLIC.search(p):
            out["ipa"] = p
            rest.remove(p)
            break
    for i, p in enumerate(rest):
        m = RU_READ_RE.search(p)
        if m and not out["ru_read"] and _looks_like_reading(m.group(1), out["word"]):
            out["ru_read"] = m.group(1).strip()
            rest[i] = p[:m.start()].strip()
    rest = [p for p in rest if p]
    if rest:
        out["translation"] = rest[0]
    if len(rest) > 1:
        out["example_en"] = rest[1]
    if len(rest) > 2:
        out["example_ru"] = rest[2]
    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/theme.css")
def theme_css():
    """Общая палитра для страниц — тот же источник, что и у карточки."""
    return app.response_class(theme.css_vars(), mimetype="text/css")


@app.get("/favicon.ico")
def favicon():
    # заглушка, чтобы браузер не сыпал 404 в консоль
    return ("", 204)


@app.route("/train")
def train():
    return render_template("train.html")


@app.get("/api/session")
def api_session():
    """Карточки для тренировки: данные + заранее собранные варианты для квиза."""
    tag = (request.args.get("deck") or "").strip()
    mode = request.args.get("mode") or "selftest"
    limit = min(100, max(3, int(request.args.get("limit") or 20)))

    if (request.args.get("only") or "") == "unreviewed":
        # разбор накопленного: слова, которые мелькали, но ответа не получили
        rows = srs.unreviewed_words(limit=limit)
    else:
        rows = srs.session_words(limit=limit, tag=tag, only_verbs=(mode == "forms"))
    out = []
    for r in rows:
        d = _with_reading(r)
        if mode == "quiz":
            d["options"] = quiz.translation_options(r)
        elif mode == "forms":
            d["options"] = quiz.form_options(r)
        out.append(d)
    return jsonify(out)


@app.post("/api/answer")
def api_answer():
    """Ответ из тренировки — попадает в ту же статистику, что и всплывашки."""
    d = request.get_json(force=True)
    wid = int(d.get("word_id"))
    action = d.get("action")
    if action not in ("know", "again", "skip"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    try:
        db.log_event(wid, action, int(d.get("ms") or 0),
                     source=("train" if d.get("source") == "train" else "popup"))
        srs.grade(wid, action)
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "word deleted"}), 404
    return jsonify({"ok": True})


@app.get("/api/progress")
def api_progress():
    """Цель дня, серия и сколько всего накопилось на разбор."""
    return jsonify({
        **srs.goal_progress(),
        "unreviewed": srs.unreviewed_count(),
        "never_shown": srs.never_shown_count(),
        "threshold": db.get_int("review_threshold", 25),
    })


@app.post("/api/session/finish")
def api_session_finish():
    """Итог пройденной тренировки. Отдельные ответы уже записаны — здесь
    фиксируется сама сессия, иначе по событиям не видно, где она кончилась."""
    d = request.get_json(force=True) or {}
    try:
        sid = db.log_session(
            mode=d.get("mode", ""), deck=d.get("deck", ""),
            total=d.get("total", 0), right_cnt=d.get("right", 0),
            wrong_cnt=d.get("wrong", 0), skipped=d.get("skipped", 0),
            seconds=d.get("seconds", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad payload"}), 400
    return jsonify({"ok": True, "id": sid})


@app.get("/api/sessions")
def api_sessions():
    """История тренировок и сводка по режимам."""
    c = db.conn()
    day = srs.LOCAL_DAY.replace("shown_at", "finished_at")
    today = datetime.now().strftime("%Y-%m-%d")

    recent = [dict(r) for r in c.execute(
        f"""SELECT id, mode, deck, total, right_cnt, wrong_cnt, skipped, seconds,
                   datetime(finished_at,'localtime') at, {day} d
            FROM sessions ORDER BY id DESC LIMIT 15""")]

    by_mode = [dict(r) for r in c.execute(
        """SELECT mode, COUNT(*) sessions, SUM(total) cards,
                  SUM(right_cnt) right_cnt, SUM(wrong_cnt) wrong_cnt,
                  SUM(seconds) seconds
           FROM sessions GROUP BY mode ORDER BY cards DESC""")]

    q = lambda sql, p=(): c.execute(sql, p).fetchone()[0]
    total_cards = q("SELECT COALESCE(SUM(total),0) FROM sessions")
    total_right = q("SELECT COALESCE(SUM(right_cnt),0) FROM sessions")
    total_wrong = q("SELECT COALESCE(SUM(wrong_cnt),0) FROM sessions")
    answered = total_right + total_wrong

    days = []
    agg = {r["d"]: r["n"] for r in c.execute(
        f"SELECT {day} d, COALESCE(SUM(total),0) n FROM sessions GROUP BY d")}
    for i in range(13, -1, -1):
        dd = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        days.append({"date": dd, "cards": agg.get(dd, 0)})

    return jsonify({
        "sessions": q("SELECT COUNT(*) FROM sessions"),
        "sessions_today": q(f"SELECT COUNT(*) FROM sessions WHERE {day}=?", (today,)),
        "cards": total_cards,
        "cards_today": q(f"SELECT COALESCE(SUM(total),0) FROM sessions WHERE {day}=?", (today,)),
        "right": total_right,
        "wrong": total_wrong,
        "accuracy": round(total_right / answered * 100) if answered else 0,
        "minutes": round(q("SELECT COALESCE(SUM(seconds),0) FROM sessions") / 60),
        "answers_from_train": q("SELECT COUNT(*) FROM events WHERE source='train'"),
        "answers_from_popup": q("SELECT COUNT(*) FROM events WHERE source='popup'"),
        "by_mode": by_mode,
        "recent": recent,
        "days": days,
    })


@app.get("/api/queue")
def api_queue():
    """Слова, отмеченные кликом, но ещё без перевода."""
    rows = [dict(r) for r in db.conn().execute(
        """SELECT id, word, note, datetime(created_at,'localtime') at
           FROM words WHERE translation = '' ORDER BY created_at""")]
    return jsonify({"items": rows, "ai_enabled": ai.is_enabled()})


@app.post("/api/queue/<int:wid>")
def api_queue_fill(wid):
    """Перевод вручную — чтобы слово не ждало, пока освободится нейросеть."""
    d = request.get_json(force=True) or {}
    tr = (d.get("translation") or "").strip()
    if not tr:
        return jsonify({"ok": False, "error": "нужен перевод"}), 400
    c = db.conn()
    row = c.execute("SELECT word FROM words WHERE id=?", (wid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "слово не найдено"}), 404
    c.execute("UPDATE words SET translation=?, ipa=?, enriched=1 WHERE id=?",
              (tr, (d.get("ipa") or "").strip(), wid))
    c.commit()
    return jsonify({"ok": True, "word": row["word"]})


@app.delete("/api/queue/<int:wid>")
def api_queue_drop(wid):
    c = db.conn()
    c.execute("DELETE FROM words WHERE id=? AND translation=''", (wid,))
    c.commit()
    return jsonify({"ok": True})


@app.post("/api/queue/run")
def api_queue_run():
    """Разобрать очередь прямо сейчас, не дожидаясь фонового помощника."""
    if not ai.is_enabled():
        return jsonify({"ok": False, "error": "нейросеть выключена в настройках"}), 400
    rows = aiworker.pending_unknown(3)
    if not rows:
        return jsonify({"ok": True, "done": 0, "message": "очередь пуста"})
    done, errors = 0, []
    for row in rows:
        try:
            done += aiworker.do_unknown(row)
        except ai.RateLimited:
            errors.append("сервис ограничил частоту запросов — попробуйте позже")
            break
        except Exception as e:
            errors.append(f"{row['word']}: {type(e).__name__}")
    return jsonify({"ok": True, "done": done, "errors": errors})


@app.get("/api/decks")
def api_decks():
    tags = {}
    for r in db.conn().execute("SELECT tags FROM words WHERE tags != ''"):
        for t in r["tags"].split(","):
            t = t.strip()
            if t:
                tags[t] = tags.get(t, 0) + 1
    return jsonify(sorted(tags.items(), key=lambda x: -x[1]))


# Сколько дней показывать в отчёте. Год целиком столбиками не читается,
# поэтому длинные периоды сворачиваются в недели и месяцы.
PERIODS = {7: "day", 30: "day", 90: "week", 365: "month"}


@app.get("/api/stats")
def api_stats():
    s = srs.stats()
    c = db.conn()
    try:
        span = int(request.args.get("days") or 30)
    except ValueError:
        span = 30
    span = span if span in PERIODS else 30

    # Один запрос с группировкой по локальному дню вместо N отдельных.
    agg = {r["d"]: r for r in c.execute(
        f"""SELECT {srs.LOCAL_DAY} d, COUNT(*) a, SUM(action='know') k
            FROM events GROUP BY d""")}
    days = []
    for i in range(span - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        r = agg.get(d)
        days.append({"date": d,
                     "shown": (r["a"] if r else 0) or 0,
                     "know": (r["k"] if r else 0) or 0})
    days = _bucket(days, PERIODS[span])

    hard = [dict(r) for r in c.execute("""
        SELECT w.id, w.word, w.translation,
               SUM(e.action='again') again_cnt,
               SUM(e.action='know')  know_cnt,
               COUNT(*) shown_cnt
        FROM events e JOIN words w ON w.id = e.word_id
        GROUP BY w.id
        HAVING again_cnt > 0
        ORDER BY again_cnt DESC, shown_cnt DESC
        LIMIT 15""")]

    tags = {}
    for r in c.execute("SELECT tags FROM words WHERE tags != ''"):
        for t in r["tags"].split(","):
            t = t.strip()
            if t:
                tags[t] = tags.get(t, 0) + 1

    # «Почему одни и те же слова?» — вопрос решается не ощущением, а списком:
    # вот слова, которые мелькают чаще всех, и вот сколько раз вы на них
    # ответили. Пустой столбец ответов и есть причина, по которой они не уходят.
    spinning = [dict(r) for r in c.execute("""
        SELECT w.id, w.word, w.translation, w.tags,
               COUNT(e.id) shown_cnt,
               SUM(e.action='know')  know_cnt,
               SUM(e.action='again') again_cnt,
               COALESCE(sr.status,'new') status
        FROM events e JOIN words w ON w.id = e.word_id
        LEFT JOIN srs sr ON sr.word_id = w.id
        GROUP BY w.id ORDER BY shown_cnt DESC LIMIT 15""")]

    return jsonify({"stats": s, "days": days, "hard": hard, "spinning": spinning,
                    "focus_tag": (db.get("focus_tag") or "").strip(),
                    "span": span, "bucket": PERIODS[span],
                    "period": _period_totals(c, span),
                    "tags": sorted(tags.items(), key=lambda x: -x[1]),
                    **extended_stats(c)})


def _bucket(days, size):
    """Сворачивает дни в недели или месяцы.

    365 столбиков в полосе шириной с экран — это полоса шума: разницу между
    соседними днями всё равно не видно, а сезон виден только крупным шагом.
    """
    if size == "day":
        return days
    out, cur = [], {}
    for d in days:
        key = d["date"][:7] if size == "month" else _week_start(d["date"])
        if cur.get("date") != key:
            cur = {"date": key, "shown": 0, "know": 0}
            out.append(cur)
        cur["shown"] += d["shown"]
        cur["know"] += d["know"]
    return out


def _week_start(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def _period_totals(c, span):
    """Итоги ровно за выбранный период, а не за всё время."""
    since = (datetime.now() - timedelta(days=span - 1)).strftime("%Y-%m-%d")
    day = srs.LOCAL_DAY
    one = lambda sql, p=(): c.execute(sql, p).fetchone()[0] or 0
    return {
        "days": span,
        "since": since,
        "shown": one(f"SELECT COUNT(*) FROM events WHERE {day} >= ?", (since,)),
        "answered": one(f"SELECT COUNT(*) FROM events WHERE action IN ('know','again') AND {day} >= ?", (since,)),
        "know": one(f"SELECT COUNT(*) FROM events WHERE action='know' AND {day} >= ?", (since,)),
        "again": one(f"SELECT COUNT(*) FROM events WHERE action='again' AND {day} >= ?", (since,)),
        "words_touched": one(f"SELECT COUNT(DISTINCT word_id) FROM events WHERE {day} >= ?", (since,)),
        "learned": one("""SELECT COUNT(*) FROM srs WHERE status='learned'
                          AND substr(datetime(learned_at,'localtime'),1,10) >= ?""", (since,)),
        "added": one("""SELECT COUNT(*) FROM words
                        WHERE substr(datetime(created_at,'localtime'),1,10) >= ?""", (since,)),
        "active_days": one(f"SELECT COUNT(DISTINCT {day}) FROM events WHERE {day} >= ?", (since,)),
        "minutes": round(one(f"SELECT SUM(ms_visible) FROM events WHERE {day} >= ?", (since,)) / 60000.0, 1),
    }


@app.get("/api/learned")
def api_learned():
    """Все выученные слова — целиком, а не последние несколько."""
    return jsonify({"items": srs.learned_words(
        order=request.args.get("order") or "learned_at")})


@app.post("/api/word/<int:wid>/relearn")
def api_relearn(wid):
    """Вернуть слово в изучение: забылось — пусть показывается снова."""
    if not db.conn().execute("SELECT 1 FROM words WHERE id=?", (wid,)).fetchone():
        return jsonify({"ok": False, "error": "слово не найдено"}), 404
    row = srs.relearn(wid)
    return jsonify({"ok": True, "status": row["status"], "reps": row["reps"]})


@app.get("/api/word/<int:wid>/history")
def api_word_history(wid):
    h = srs.word_history(wid)
    if h is None:
        return jsonify({"error": "слово не найдено"}), 404
    h["ru_read"] = ruread.show(h)
    return jsonify(h)


def extended_stats(c):
    """Срезы, которых не хватало: прогресс по колодам, прогноз, ритм ответов."""
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    now = db.now_iso()
    day = srs.LOCAL_DAY

    # --- прогресс по каждой колоде ---
    decks = {}
    for r in c.execute("""SELECT w.tags, COALESCE(s.status,'new') st FROM words w
                          LEFT JOIN srs s ON s.word_id = w.id WHERE w.tags != ''"""):
        for t in r["tags"].split(","):
            t = t.strip()
            if not t:
                continue
            d = decks.setdefault(t, {"deck": t, "new": 0, "learning": 0, "learned": 0, "total": 0})
            d[r["st"]] = d.get(r["st"], 0) + 1
            d["total"] += 1
    decks = sorted(decks.values(), key=lambda d: -d["total"])

    # --- сколько слов доведено до «выучено» по дням ---
    learned_days = []
    running = 0
    daily = {r["d"]: r["n"] for r in c.execute(
        f"""SELECT {day} d, COUNT(DISTINCT word_id) n FROM events
            WHERE action='know' GROUP BY d""")}
    for i in range(29, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        running += daily.get(d, 0)
        learned_days.append({"date": d, "count": daily.get(d, 0), "total": running})

    # --- из чего складываются ответы ---
    actions = {r["action"]: r["n"] for r in c.execute(
        "SELECT action, COUNT(*) n FROM events GROUP BY action")}

    # --- когда вы отвечаете: активность по часам ---
    hours = [0] * 24
    for r in c.execute(
            "SELECT CAST(strftime('%H', datetime(shown_at,'localtime')) AS INT) h, COUNT(*) n "
            "FROM events GROUP BY h"):
        if r["h"] is not None:
            hours[r["h"]] = r["n"]

    # --- что созреет в ближайшие дни ---
    forecast = []
    for i in range(0, 7):
        start = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
        n = c.execute(
            f"""SELECT COUNT(*) FROM srs
                WHERE status IN ('learning','learned')
                  AND substr(datetime(due_at,'localtime'),1,10) = ?""", (start,)).fetchone()[0]
        forecast.append({"date": start, "count": n})

    q = lambda sql, p=(): c.execute(sql, p).fetchone()[0]
    extra = {
        "learned_today": q(f"""SELECT COUNT(*) FROM srs s WHERE s.status='learned'
            AND s.word_id IN (SELECT word_id FROM events WHERE action='know' AND {day}=?)""", (today,)),
        "learned_week": q(f"""SELECT COUNT(*) FROM srs s WHERE s.status='learned'
            AND s.word_id IN (SELECT word_id FROM events WHERE action='know' AND {day}>=?)""", (week_ago,)),
        "answers_today": q(f"SELECT COUNT(*) FROM events WHERE action IN ('know','again') AND {day}=?", (today,)),
        "avg_answer_ms": q("SELECT COALESCE(AVG(ms_visible),0) FROM events WHERE action IN ('know','again') AND ms_visible > 300"),
        "due_today": q("""SELECT COUNT(*) FROM srs WHERE status IN ('learning','learned')
                          AND due_at <= ?""", (now,)),
        "words_touched": q("SELECT COUNT(DISTINCT word_id) FROM events"),
        "unreviewed": srs.unreviewed_count(),
        "never_shown": srs.never_shown_count(),
    }
    extra["goal"] = srs.goal_progress()

    # --- последние доведённые до конца ---
    recent_learned = [dict(r) for r in c.execute("""
        SELECT w.word, w.translation, w.tags, s.reps,
               datetime(s.due_at,'localtime') next_at
        FROM srs s JOIN words w ON w.id = s.word_id
        WHERE s.status='learned' ORDER BY s.word_id DESC LIMIT 12""")]

    return {"decks": decks, "learned_days": learned_days, "actions": actions,
            "hours": hours, "forecast": forecast, "extra": extra,
            "recent_learned": recent_learned}


@app.get("/api/words")
def api_words():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    tag = (request.args.get("tag") or "").strip()

    sql = """SELECT w.*, COALESCE(s.status,'new') status,
                    COALESCE(s.reps,0) reps, COALESCE(s.lapses,0) lapses,
                    s.due_at, COALESCE(s.interval_min,0) interval_min,
                    datetime(w.created_at,'localtime')     added_at,
                    datetime(s.first_shown_at,'localtime') first_shown_at,
                    datetime(s.learned_at,'localtime')     learned_at,
                    (SELECT COUNT(*) FROM events e WHERE e.word_id=w.id) shown_cnt,
                    (SELECT COUNT(*) FROM events e WHERE e.word_id=w.id AND e.action='know') know_cnt,
                    (SELECT COUNT(*) FROM events e WHERE e.word_id=w.id AND e.action='again') again_cnt,
                    datetime((SELECT MAX(e.shown_at) FROM events e WHERE e.word_id=w.id),
                             'localtime') last_shown_at,
                    -- сколько суток слово в работе: от первой встречи до
                    -- «выучено», а пока не выучено — до сегодня
                    CAST(julianday(COALESCE(NULLIF(s.learned_at,''), 'now')) -
                         julianday(NULLIF(s.first_shown_at,'')) AS INTEGER) days_learning
             FROM words w LEFT JOIN srs s ON s.word_id=w.id WHERE 1=1"""
    params = []
    if q:
        sql += " AND (w.word LIKE ? OR w.translation LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if status:
        sql += " AND s.status = ?"
        params.append(status)
    if tag:
        sql += " AND w.tags LIKE ?"
        params.append(f"%{tag}%")
    order = {"new": "w.created_at DESC, w.id DESC",
             "shown": "shown_cnt DESC",
             "rare": "shown_cnt ASC",
             "word": "w.word COLLATE NOCASE ASC",
             "know": "know_cnt DESC",
             "days": "days_learning DESC"}.get(request.args.get("sort") or "new")
    sql += f" ORDER BY {order or 'w.created_at DESC, w.id DESC'} LIMIT 800"
    return jsonify([_with_reading(r) for r in db.conn().execute(sql, params)])


@app.post("/api/import")
def api_import():
    data = request.get_json(force=True)
    text = data.get("text", "")
    tags = (data.get("tags") or "").strip()
    level = (data.get("level") or "").strip()

    created = updated = skipped = 0
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        p = parse_line(line)
        if not p:
            skipped += 1
            continue
        _, res = db.add_word(p["word"], ipa=p["ipa"], translation=p["translation"],
                             example_en=p["example_en"], example_ru=p["example_ru"],
                             level=level, tags=tags, ru_read=p["ru_read"])
        if res == "created":
            created += 1
        elif res == "updated":
            updated += 1
        else:
            skipped += 1
    return jsonify({"created": created, "updated": updated, "skipped": skipped})


@app.post("/api/word/<int:wid>")
def api_word_update(wid):
    d = request.get_json(force=True)
    fields = ["word", "ipa", "translation", "example_en", "example_ru",
              "level", "tags", "note", "ru_read"]
    sets, params = [], []
    for f in fields:
        if f in d:
            sets.append(f"{f}=?")
            params.append((d[f] or "").strip())
    if sets:
        params.append(wid)
        c = db.conn()
        c.execute(f"UPDATE words SET {', '.join(sets)} WHERE id=?", params)
        c.execute("UPDATE words SET enriched=1 WHERE id=? AND ipa!='' AND translation!=''", (wid,))
        c.commit()
    if "status" in d:
        srs.set_status(wid, d["status"])
    return jsonify({"ok": True})


@app.delete("/api/word/<int:wid>")
def api_word_delete(wid):
    c = db.conn()
    c.execute("DELETE FROM words WHERE id=?", (wid,))
    c.commit()
    return jsonify({"ok": True})


@app.get("/api/ai/status")
def api_ai_status():
    """Что нейросеть уже сделала и что стоит в очереди."""
    c = db.conn()
    q = lambda sql, p=(): c.execute(sql, p).fetchone()[0]
    return jsonify({
        "enabled": ai.is_enabled(),
        "has_key": bool((db.get("ai_key", "") or "").strip()),
        "model": db.get("ai_model"),
        "sentences_ai": q("SELECT COUNT(*) FROM sentences WHERE source='ai'"),
        "sentences_total": q("SELECT COUNT(*) FROM sentences"),
        "words_ai": q("SELECT COUNT(*) FROM words WHERE tags LIKE '%ai%'"),
        "queue": q("SELECT COUNT(*) FROM words WHERE translation=''"),
        "new_left": aiworker.new_words_left(),
        "min_new": db.get_int("ai_min_new_words", 10),
        "avg_sentences": round(q("SELECT COALESCE(AVG(n),0) FROM "
                                 "(SELECT COUNT(*) n FROM sentences GROUP BY word_id)"), 1),
        "queued_words": [dict(r) for r in c.execute(
            "SELECT word, note FROM words WHERE translation='' ORDER BY created_at LIMIT 10")],
    })


@app.post("/api/ai/test")
def api_ai_test():
    """Кнопка «Проверить связь» — сразу видно, живы ли ключ и модель."""
    import time
    t0 = time.time()
    try:
        ok = ai.check_connection()
        return jsonify({"ok": ok, "seconds": round(time.time() - t0, 1),
                        "model": db.get("ai_model")})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                        "seconds": round(time.time() - t0, 1)})


@app.get("/export.csv")
def export_csv():
    """Выгрузка словаря для Anki и других программ.

    Формат — обычный CSV с разделителем-табуляцией: Anki читает его как есть,
    первое поле лицевая сторона, второе оборот. Прогресс тоже в файле,
    чтобы вместе со словами уезжала и история.
    """
    import csv
    import io as _io

    buf = _io.StringIO()
    w = csv.writer(buf, delimiter=chr(9), lineterminator=chr(10))
    w.writerow(["word", "translation", "ipa", "ru_read", "v2", "v3",
                "example_en", "example_ru", "tags", "status", "reps"])
    rows = db.conn().execute(
        """SELECT w.word, w.translation, w.ipa, w.v2, w.v3, w.tags,
                  COALESCE(s.status,'new') status, COALESCE(s.reps,0) reps,
                  (SELECT text_en FROM sentences x WHERE x.word_id=w.id
                    ORDER BY x.shown DESC LIMIT 1) ex_en,
                  (SELECT text_ru FROM sentences x WHERE x.word_id=w.id
                    ORDER BY x.shown DESC LIMIT 1) ex_ru
           FROM words w LEFT JOIN srs s ON s.word_id = w.id
           WHERE w.translation != '' ORDER BY w.word COLLATE NOCASE""")
    for r in rows:
        w.writerow([r["word"], r["translation"], r["ipa"], ruread.show(r),
                    r["v2"], r["v3"],
                    r["ex_en"] or "", r["ex_ru"] or "", r["tags"],
                    r["status"], r["reps"]])
    data = buf.getvalue()
    return app.response_class(
        data, mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="vocab-popup.csv"'})


@app.get("/api/settings")
def api_settings_get():
    return jsonify(db.all_settings())


@app.post("/api/settings")
def api_settings_set():
    for k, v in (request.get_json(force=True) or {}).items():
        if k in db.DEFAULTS:
            db.put(k, v)
    return jsonify(db.all_settings())


def run():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=8777, debug=False,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    db.init()
    run()
