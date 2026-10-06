# -*- coding: utf-8 -*-
"""SM-2 в минутном масштабе + выбор следующего слова для показа."""
import random
from datetime import datetime, timezone, timedelta

import db

DAY = 1440

# Лестница интервалов после «Знаю»: 2 часа -> 8 часов -> сутки -> дальше × ease.
# Шаги подобраны под фоновый режим. В Anki первый шаг — 10 минут, но там человек
# сам садится за карточки раз в день; здесь показ идёт каждые несколько минут,
# и десятиминутный шаг возвращал знакомое слово уже через три карточки.
FIRST_STEPS = [120, 8 * 60, DAY]
LEARNED_AFTER_MIN = 7 * DAY   # интервал, с которого слово считается выученным
LEARNED_INTERVAL = 30 * DAY   # куда уводится слово, признанное выученным
AGAIN_DELAY = 10              # «ещё раз» — слово нужно увидеть скоро
SKIP_DELAY = 90               # непросмотренная карточка: не ответ, а «не увидел»


def _now():
    return datetime.now(timezone.utc)


def grade(word_id, action):
    """Пересчитать расписание. action: know | again | skip."""
    c = db.conn()
    row = c.execute("SELECT * FROM srs WHERE word_id=?", (word_id,)).fetchone()
    if row is None:
        c.execute("INSERT INTO srs(word_id, due_at, status) VALUES (?,?, 'new')",
                  (word_id, db.now_iso()))
        c.commit()
        row = c.execute("SELECT * FROM srs WHERE word_id=?", (word_id,)).fetchone()

    ease = row["ease"] or 2.5
    interval = row["interval_min"] or 0
    reps = row["reps"] or 0
    lapses = row["lapses"] or 0

    if action == "know":
        reps += 1
        if interval < FIRST_STEPS[-1]:
            nxt = next((s for s in FIRST_STEPS if s > interval), FIRST_STEPS[-1])
            interval = nxt
        else:
            interval = interval * ease
        ease = min(3.2, ease + 0.08)
    elif action == "again":
        lapses += 1
        ease = max(1.3, ease - 0.2)
        interval = AGAIN_DELAY
    else:
        # skip — карточка погасла сама или её отложили. Это «не посмотрел»,
        # а не «не знаю»: наказывать за это нельзя, иначе слово возвращается
        # через 10 минут и весь день крутится по кругу.
        interval = max(SKIP_DELAY, interval)
        reps = reps                      # прогресс не сбрасываем

    interval = min(interval, 180 * DAY)
    # ±10% разброса, чтобы слова не слипались в одну пачку
    jitter = interval * random.uniform(-0.1, 0.1)
    due = _now() + timedelta(minutes=interval + jitter)

    # Слово считается выученным после нескольких верных ответов подряд —
    # порог задаётся настройкой. Дальше оно не мешает: попадает в выборку,
    # только если включён контроль выученных.
    need = max(1, db.get_int("know_to_learn", 2))
    if action == "know" and reps >= need:
        status = "learned"
        interval = max(interval, LEARNED_INTERVAL)
        due = _now() + timedelta(minutes=interval)
    elif interval >= LEARNED_AFTER_MIN:
        status = "learned"
    elif action == "skip" and reps == 0:
        status = "new"          # ни разу не отвечали — слово всё ещё новое
    elif action == "skip":
        status = row["status"] or "learning"
    else:
        status = "learning"

    # Момент перехода в «выучено» записываем один раз: по событиям его потом
    # не восстановить, а без него не ответить, когда и сколько выучено.
    learned_at = row["learned_at"] if "learned_at" in row.keys() else ""
    if status == "learned" and not learned_at:
        learned_at = db.now_iso()
    elif status != "learned":
        learned_at = ""

    c.execute("""UPDATE srs SET ease=?, interval_min=?, due_at=?, reps=?, lapses=?,
                                status=?, learned_at=?
                 WHERE word_id=?""",
              (ease, interval, due.isoformat(timespec="seconds"), reps, lapses,
               status, learned_at, word_id))
    c.commit()
    return status, interval


def set_status(word_id, status):
    """Ручное 'знаю совсем' / 'отложить' из дашборда."""
    c = db.conn()
    if status == "learned":
        due = _now() + timedelta(minutes=30 * DAY)
        c.execute("""UPDATE srs SET status='learned', interval_min=?, due_at=?,
                                    learned_at=COALESCE(NULLIF(learned_at,''), ?)
                     WHERE word_id=?""",
                  (30 * DAY, due.isoformat(timespec="seconds"), db.now_iso(), word_id))
    elif status == "suspended":
        c.execute("UPDATE srs SET status='suspended' WHERE word_id=?", (word_id,))
    else:
        c.execute("""UPDATE srs SET status='new', interval_min=0, ease=2.5, reps=0,
                     learned_at='', due_at=? WHERE word_id=?""", (db.now_iso(), word_id))
    c.commit()


def relearn(word_id):
    """Вернуть выученное слово в изучение.

    Счётчик верных ответов обнуляется намеренно. Если его оставить, слово
    после первого же «Знаю» снова уйдёт в выученные — а вернули его как раз
    потому, что оно забылось. Ошибки (lapses) и лёгкость сохраняем: это
    история слова, и она пригодится при следующем расчёте интервала.
    """
    c = db.conn()
    c.execute("""UPDATE srs SET status='learning', reps=0, interval_min=0,
                                learned_at='', due_at=?, priority_at=?
                 WHERE word_id=?""", (db.now_iso(), db.now_iso(), word_id))
    c.commit()
    return c.execute("SELECT status, reps FROM srs WHERE word_id=?",
                     (word_id,)).fetchone()


def _pick(sql, params=()):
    rows = db.conn().execute(sql, params).fetchall()
    return random.choice(rows) if rows else None


# Слово не повторяется, пока не пройдут другие. Иначе выборка кучкуется:
# одно и то же слово выпадало по нескольку раз подряд, а часть колоды молчала.
RECENT_BLOCK = 25


def _recent_ids(pool_size):
    """Недавно показанные слова. Блокируем не больше трети колоды,
    иначе на маленькой колоде блокировать станет нечего."""
    limit = max(0, min(RECENT_BLOCK, pool_size // 3))
    if limit == 0:
        return []
    rows = db.conn().execute(
        "SELECT word_id FROM events ORDER BY id DESC LIMIT ?", (limit * 2,)).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["word_id"] not in seen:
            seen.add(r["word_id"])
            out.append(r["word_id"])
        if len(out) >= limit:
            break
    return out


def _count(clause, params, focus_clause=""):
    """Сколько слов подходит под условие — нужно, чтобы взвесить ветки выбора."""
    sql = ("""SELECT COUNT(*) FROM words w JOIN srs s ON s.word_id = w.id
              WHERE w.translation != '' AND s.status != 'suspended' """
           + focus_clause + clause)
    return db.conn().execute(sql, params).fetchone()[0]


def new_started_today():
    """Сколько новых слов уже начато сегодня — по первому показу каждого слова."""
    today = datetime.now().strftime("%Y-%m-%d")
    return db.conn().execute(
        """SELECT COUNT(*) FROM (SELECT word_id, MIN(shown_at) m FROM events GROUP BY word_id)
           WHERE substr(datetime(m, 'localtime'), 1, 10) = ?""", (today,)).fetchone()[0]


def new_quota_left():
    """Остаток дневной порции. 0 или меньше — новые слова на сегодня закончились."""
    limit = db.get_int("new_per_day", 20)
    if limit <= 0:
        return 10 ** 6            # 0 в настройке = без ограничения
    return limit - new_started_today()


def _pool_size(focus):
    sql = """SELECT COUNT(*) FROM words w JOIN srs s ON s.word_id = w.id
             WHERE w.translation != '' AND s.status != 'suspended'"""
    params = ()
    if focus:
        sql += " AND w.tags LIKE ?"
        params = (f"%{focus}%",)
    return db.conn().execute(sql, params).fetchone()[0]


def _build(clause, exclude):
    """Запрос с исключением недавно показанных.

    Порядок плейсхолдеров важен: сначала список исключений, затем фокус-тег,
    затем условие по сроку — параметры собираются в вызывающем коде так же.
    """
    base = """SELECT w.*, s.status, s.interval_min, s.reps
              FROM words w JOIN srs s ON s.word_id = w.id
              WHERE w.translation != '' AND s.status != 'suspended' """
    if exclude:
        base += " AND w.id NOT IN (%s) " % ",".join("?" * len(exclude))
    return base + clause


def next_word():
    """Следующее слово: просроченные повторы, новые и контроль выученного.

    Новые слова берутся в случайном порядке, а не по дате добавления: их срок
    совпадает с моментом создания, и сортировка по сроку всегда возвращала бы
    одну и ту же голову списка — остальная колода не показывалась бы никогда.
    """
    focus = (db.get("focus_tag") or "").strip()
    now = db.now_iso()
    use_focus = bool(focus) and random.random() < 0.8
    focus_clause = " AND w.tags LIKE ? " if use_focus else ""
    focus_param = (f"%{focus}%",) if use_focus else ()

    recent = _recent_ids(_pool_size(focus if use_focus else ""))
    quota_left = new_quota_left()

    # Доля повторов зависит от того, сколько их реально созрело. Фиксированные
    # 55% при семи просроченных словах означали бы, что больше половины показов
    # крутится вокруг этой семёрки, пока две сотни новых ждут своей очереди.
    due_learning = _count(" AND s.due_at <= ? AND s.status='learning'",
                          focus_param + (now,), focus_clause)
    due_learned = _count(" AND s.due_at <= ? AND s.status='learned'",
                         focus_param + (now,), focus_clause)
    p_learning = min(0.40, due_learning / 60.0)
    review_learned = db.get("review_learned", "0") == "1"
    p_learned = min(0.10, due_learned / 60.0) if review_learned else 0.0

    # Возвращённое руками слово показываем первым и ровно один раз вне очереди:
    # дальше оно живёт по общим правилам.
    pri = db.conn().execute("""
        SELECT w.*, s.status, s.interval_min, s.reps
        FROM words w JOIN srs s ON s.word_id = w.id
        WHERE s.priority_at != '' AND w.translation != '' AND s.status != 'suspended'
        ORDER BY s.priority_at LIMIT 1""").fetchone()
    if pri is not None:
        db.write("UPDATE srs SET priority_at='' WHERE word_id=?", (pri["id"],))
        return pri

    for exclude in (recent, []):
        r = random.random()
        variants = []                       # (условие, доп. параметры)
        if r < p_learning:
            variants.append((focus_clause + " AND s.due_at <= ? AND s.status='learning' "
                             "ORDER BY s.due_at LIMIT 30", (now,)))
        elif r < p_learning + p_learned:
            variants.append((focus_clause + " AND s.due_at <= ? AND s.status='learned' "
                             "ORDER BY RANDOM() LIMIT 30", (now,)))
        # Основная масса показов — новые слова, в случайном порядке.
        # Но не больше дневной порции: иначе за день пролетает весь словарь,
        # и ничего не успевает закрепиться.
        if quota_left > 0:
            variants.append((focus_clause + " AND s.status='new' ORDER BY RANDOM() LIMIT 30", ()))
        # Запасные ветки: выученные сюда не попадают, если контроль выключен.
        tail = " AND s.status != 'learned' " if not review_learned else ""
        variants.append((focus_clause + tail + " AND s.due_at <= ? ORDER BY RANDOM() LIMIT 30", (now,)))
        variants.append((focus_clause + tail + " AND s.status='new' ORDER BY RANDOM() LIMIT 30", ()))
        variants.append((focus_clause + tail + " ORDER BY RANDOM() LIMIT 30", ()))

        for clause, extra in variants:
            row = _pick(_build(clause, exclude),
                        tuple(exclude) + focus_param + extra)
            if row:
                return row

    # Фокус-колода пуста — берём любое слово из словаря. Выученные сюда
    # не попадают: «больше не показывать» должно работать и в этом случае,
    # иначе на исходе колоды они начинали всплывать снова.
    tail = "" if db.get("review_learned", "0") == "1" else " AND s.status != 'learned' "
    return _pick(f"""SELECT w.*, s.status, s.interval_min, s.reps
                     FROM words w JOIN srs s ON s.word_id = w.id
                     WHERE w.translation != '' AND s.status != 'suspended' {tail}
                     ORDER BY RANDOM() LIMIT 30""")


# Время в базе хранится в UTC, а «сегодня» человек понимает по своим часам.
# Поэтому день события всегда берём через localtime, иначе с полуночи до утра
# статистика показывала бы вчерашний день.
LOCAL_DAY = "substr(datetime(shown_at, 'localtime'), 1, 10)"


def unreviewed_count():
    """Сколько слов показывалось, но так и не получило ответа.

    Именно они — главная утечка: карточка мелькнула, слово не сдвинулось.
    Когда их набирается много, стоит предложить разобрать пачку за раз.
    """
    return db.conn().execute(
        """SELECT COUNT(*) FROM words w JOIN srs s ON s.word_id = w.id
           WHERE s.reps = 0 AND s.status != 'suspended' AND w.translation != ''
             AND EXISTS (SELECT 1 FROM events e WHERE e.word_id = w.id)""").fetchone()[0]


def unreviewed_words(limit=20):
    """Те же слова списком — для разбора: сперва показанные чаще всего."""
    return db.conn().execute(
        """SELECT w.*, s.status, s.due_at,
                  (SELECT COUNT(*) FROM events e WHERE e.word_id = w.id) shows
           FROM words w JOIN srs s ON s.word_id = w.id
           WHERE s.reps = 0 AND s.status != 'suspended' AND w.translation != ''
             AND EXISTS (SELECT 1 FROM events e WHERE e.word_id = w.id)
           ORDER BY shows DESC, s.due_at LIMIT ?""", (limit,)).fetchall()


def never_shown_count():
    return db.conn().execute(
        """SELECT COUNT(*) FROM words w
           WHERE w.translation != ''
             AND NOT EXISTS (SELECT 1 FROM events e WHERE e.word_id = w.id)""").fetchone()[0]


def goal_progress():
    """Цель дня и серия дней подряд, когда цель была взята."""
    goal = max(0, db.get_int("daily_goal", 5))
    c = db.conn()
    today = datetime.now().strftime("%Y-%m-%d")
    done = c.execute(
        f"SELECT COUNT(DISTINCT word_id) FROM events WHERE action='know' AND {LOCAL_DAY}=?",
        (today,)).fetchone()[0]

    by_day = {r["d"]: r["n"] for r in c.execute(
        f"""SELECT {LOCAL_DAY} d, COUNT(DISTINCT word_id) n FROM events
            WHERE action='know' GROUP BY d""")}
    streak = 0
    day = datetime.now()
    while goal > 0:
        key = day.strftime("%Y-%m-%d")
        hit = by_day.get(key, 0) >= goal
        if not hit:
            # сегодняшний день ещё не закончен — он не рвёт серию
            if key == today:
                day -= timedelta(days=1)
                continue
            break
        streak += 1
        day -= timedelta(days=1)
    return {"goal": goal, "done": done, "streak": streak,
            "reached": goal > 0 and done >= goal}


def session_words(limit=20, tag="", only_verbs=False):
    """Набор карточек для тренировки: сперва просроченные, затем новые.

    В отличие от next_word здесь не нужна случайность — сессия конечная,
    и важно за неё пройти самое нужное.
    """
    sql = """SELECT w.*, s.status, s.due_at FROM words w JOIN srs s ON s.word_id = w.id
             WHERE w.translation != '' AND s.status != 'suspended' """
    params = []
    if tag:
        sql += " AND w.tags LIKE ? "
        params.append(f"%{tag}%")
    if only_verbs:
        sql += " AND w.v2 != '' "
    sql += """ ORDER BY CASE WHEN s.due_at <= ? THEN 0 ELSE 1 END,
                        CASE s.status WHEN 'learning' THEN 0 WHEN 'new' THEN 1 ELSE 2 END,
                        s.due_at
               LIMIT ?"""
    params += [db.now_iso(), int(limit)]
    return db.conn().execute(sql, params).fetchall()


def stats():
    c = db.conn()
    q = lambda s, p=(): c.execute(s, p).fetchone()[0]
    now = db.now_iso()
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "total":     q("SELECT COUNT(*) FROM words"),
        "new":       q("SELECT COUNT(*) FROM srs WHERE status='new'"),
        "learning":  q("SELECT COUNT(*) FROM srs WHERE status='learning'"),
        "learned":   q("SELECT COUNT(*) FROM srs WHERE status='learned'"),
        "suspended": q("SELECT COUNT(*) FROM srs WHERE status='suspended'"),
        "due_now":   q("SELECT COUNT(*) FROM srs WHERE due_at<=? AND status IN ('new','learning')", (now,)),
        "shown_today": q(f"SELECT COUNT(*) FROM events WHERE {LOCAL_DAY}=?", (today,)),
        "know_today":  q(f"SELECT COUNT(*) FROM events WHERE action='know' AND {LOCAL_DAY}=?", (today,)),
        "shown_total": q("SELECT COUNT(*) FROM events"),
        "no_translation": q("SELECT COUNT(*) FROM words WHERE translation=''"),
    }


def learned_words(order="learned_at", limit=0):
    """Все выученные слова с датой и историей показов.

    Список намеренно полный: раньше показывались последние 12 по номеру
    в базе, и слово, выученное сегодня, в него не попадало, если его
    добавили в словарь давно.
    """
    col = {"learned_at": "s.learned_at", "word": "w.word",
           "shown": "shown_cnt", "days": "days_to_learn"}.get(order, "s.learned_at")
    direction = "ASC" if col == "w.word" else "DESC"
    sql = f"""
        SELECT w.id, w.word, w.translation, w.ipa, w.ru_read, w.tags,
               s.reps, s.lapses,
               datetime(s.learned_at,'localtime')   learned_at,
               datetime(s.first_shown_at,'localtime') first_shown_at,
               datetime(w.created_at,'localtime')   created_at,
               datetime(s.due_at,'localtime')       due_at,
               (SELECT COUNT(*) FROM events e WHERE e.word_id=w.id) shown_cnt,
               (SELECT COUNT(*) FROM events e WHERE e.word_id=w.id AND e.action='know') know_cnt,
               (SELECT COUNT(*) FROM events e WHERE e.word_id=w.id AND e.action='again') again_cnt,
               CAST(julianday(NULLIF(s.learned_at,'')) -
                    julianday(NULLIF(s.first_shown_at,'')) AS INTEGER) days_to_learn
        FROM srs s JOIN words w ON w.id = s.word_id
        WHERE s.status = 'learned'
        ORDER BY {col} {direction}, s.word_id DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in db.conn().execute(sql)]


def word_history(word_id):
    """Вся история одного слова: когда добавлено, сколько раз показано,
    что отвечали и в какие дни. Без этого непонятно, почему слово
    продолжает всплывать."""
    c = db.conn()
    row = c.execute(f"""
        SELECT w.id, w.word, w.translation, w.ipa, w.ru_read, w.tags, w.level,
               w.example_en, w.example_ru, w.note,
               COALESCE(s.status,'new') status, COALESCE(s.reps,0) reps,
               COALESCE(s.lapses,0) lapses, COALESCE(s.ease,2.5) ease,
               COALESCE(s.interval_min,0) interval_min,
               datetime(w.created_at,'localtime')     created_at,
               datetime(s.first_shown_at,'localtime') first_shown_at,
               datetime(s.learned_at,'localtime')     learned_at,
               datetime(s.due_at,'localtime')         due_at
        FROM words w LEFT JOIN srs s ON s.word_id = w.id
        WHERE w.id = ?""", (word_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)

    counts = {r["action"]: r["n"] for r in c.execute(
        "SELECT action, COUNT(*) n FROM events WHERE word_id=? GROUP BY action",
        (word_id,))}
    out["shown_cnt"] = sum(counts.values())
    out["know_cnt"] = counts.get("know", 0)
    out["again_cnt"] = counts.get("again", 0)
    out["skip_cnt"] = counts.get("skip", 0)

    out["last_shown_at"] = (c.execute(
        "SELECT datetime(MAX(shown_at),'localtime') FROM events WHERE word_id=?",
        (word_id,)).fetchone()[0] or "")
    out["by_day"] = [dict(r) for r in c.execute(f"""
        SELECT {LOCAL_DAY} d, COUNT(*) shown, SUM(action='know') know,
               SUM(action='again') again
        FROM events WHERE word_id=? GROUP BY d ORDER BY d DESC LIMIT 60""",
        (word_id,))]
    out["recent"] = [dict(r) for r in c.execute("""
        SELECT datetime(shown_at,'localtime') at, action, source,
               ROUND(ms_visible/1000.0, 1) seconds
        FROM events WHERE word_id=? ORDER BY id DESC LIMIT 25""", (word_id,))]
    return out
