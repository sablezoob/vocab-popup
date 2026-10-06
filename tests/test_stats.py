# -*- coding: utf-8 -*-
"""Статистика: даты, периоды и возврат выученного слова в изучение.

Раньше дашборд показывал последние 12 выученных слов, упорядоченные по номеру
в базе, — слово, выученное сегодня, в этот список могло не попасть вовсе.
А момент «выучено» нигде не записывался, поэтому вопрос «сколько я выучил
за месяц» ответа не имел.
"""
from datetime import datetime, timedelta

import db
import srs


def answer(wid, action, times=1):
    for _ in range(times):
        db.log_event(wid, action)
        srs.grade(wid, action)


def test_learned_at_is_written_on_transition(fresh_db, words):
    wid = words["draw"]
    answer(wid, "know", 2)
    row = fresh_db.conn().execute("SELECT status, learned_at FROM srs WHERE word_id=?",
                                  (wid,)).fetchone()
    assert row["status"] == "learned"
    assert row["learned_at"], "без даты не ответить, когда слово было выучено"


def test_first_show_is_remembered(fresh_db, words):
    """Слово может лежать в словаре месяц и ни разу не показаться — срок
    изучения считается от первой встречи, а не от даты добавления."""
    wid = words["draw"]
    assert fresh_db.conn().execute(
        "SELECT first_shown_at FROM srs WHERE word_id=?", (wid,)).fetchone()[0] == ""
    answer(wid, "skip")
    assert fresh_db.conn().execute(
        "SELECT first_shown_at FROM srs WHERE word_id=?", (wid,)).fetchone()[0]


def test_relearn_requires_the_full_path_again(fresh_db, words):
    """Если оставить счётчик верных ответов, слово после первого же «Знаю»
    снова уйдёт в выученные — а вернули его потому, что забылось."""
    wid = words["draw"]
    answer(wid, "know", 2)
    srs.relearn(wid)

    row = fresh_db.conn().execute(
        "SELECT status, reps, learned_at FROM srs WHERE word_id=?", (wid,)).fetchone()
    assert row["status"] == "learning"
    assert row["reps"] == 0
    assert row["learned_at"] == "", "дата должна сняться вместе со статусом"

    answer(wid, "know")
    assert fresh_db.conn().execute(
        "SELECT status FROM srs WHERE word_id=?", (wid,)).fetchone()[0] != "learned"
    answer(wid, "know")
    assert fresh_db.conn().execute(
        "SELECT status FROM srs WHERE word_id=?", (wid,)).fetchone()[0] == "learned"


def test_returned_word_is_shown_right_away(fresh_db, words):
    """Вернули — значит, слово должно появиться сразу, а не когда-нибудь.

    По общим правилам оно всплыло бы часов через шесть: только что отвеченное
    блокируется списком недавних, а один созревший повтор забирает меньше
    двух процентов показов. Явное действие пользователя идёт вне очереди.
    """
    wid = words["draw"]
    answer(wid, "know", 2)
    assert "draw" not in {srs.next_word()["word"] for _ in range(60)}

    srs.relearn(wid)
    assert srs.next_word()["word"] == "draw", "первым же показом"
    # и ровно один раз: дальше слово живёт по общему расписанию
    assert fresh_db.conn().execute(
        "SELECT priority_at FROM srs WHERE word_id=?", (wid,)).fetchone()[0] == ""


def test_priority_does_not_stick(fresh_db, words):
    """Иначе вернувшееся слово заняло бы собой все показы подряд."""
    srs.relearn(words["draw"])
    shown = [srs.next_word()["word"] for _ in range(6)]
    assert shown[0] == "draw"
    assert shown.count("draw") == 1, f"повис в приоритете: {shown}"


def test_learned_list_is_complete(fresh_db, words):
    for name in ("draw", "hide", "write", "speak", "give"):
        answer(words[name], "know", 2)
    got = srs.learned_words()
    assert len(got) == 5, "список выученных не должен обрезаться"
    assert {w["word"] for w in got} == {"draw", "hide", "write", "speak", "give"}


def test_learned_list_sorted_by_date_newest_first(fresh_db, words):
    answer(words["draw"], "know", 2)
    answer(words["hide"], "know", 2)
    assert [w["word"] for w in srs.learned_words()][0] == "hide"
    assert [w["word"] for w in srs.learned_words(order="word")][0] == "draw"


def test_word_history_counts_every_kind_of_answer(fresh_db, words):
    wid = words["draw"]
    answer(wid, "skip", 3)
    answer(wid, "again")
    answer(wid, "know")
    h = srs.word_history(wid)
    assert h["shown_cnt"] == 5
    assert (h["skip_cnt"], h["again_cnt"], h["know_cnt"]) == (3, 1, 1)
    assert h["first_shown_at"] and h["last_shown_at"]
    assert h["by_day"] and h["by_day"][0]["shown"] == 5


def test_history_of_missing_word(fresh_db):
    assert srs.word_history(999999) is None


def test_backfill_recovers_dates_from_events(fresh_db, words):
    """База, заведённая до появления колонок, не должна остаться без истории."""
    wid = words["draw"]
    answer(wid, "know", 2)
    c = fresh_db.conn()
    c.execute("UPDATE srs SET learned_at='', first_shown_at='' WHERE word_id=?", (wid,))
    c.commit()

    fresh_db.backfill_history()
    row = c.execute("SELECT learned_at, first_shown_at FROM srs WHERE word_id=?",
                    (wid,)).fetchone()
    assert row["learned_at"] and row["first_shown_at"]


# ---------- веб ----------

def test_stats_accepts_known_periods(client, words):
    for days, bucket, bars in ((7, "day", 7), (30, "day", 30),
                               (90, "week", 14), (365, "month", 13)):
        d = client.get(f"/api/stats?days={days}").get_json()
        assert d["span"] == days and d["bucket"] == bucket
        assert len(d["days"]) == bars, f"{days} дней свернулись неверно"


def test_unknown_period_falls_back_to_month(client, words):
    for bad in ("999", "abc", "-5"):
        assert client.get(f"/api/stats?days={bad}").get_json()["span"] == 30


def test_period_totals_count_only_inside_the_period(client, fresh_db, words):
    wid = words["draw"]
    answer(wid, "know")
    # ответ позавчера не должен попасть в итоги за сегодняшние сутки
    old = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    c = fresh_db.conn()
    c.execute("INSERT INTO events(word_id, shown_at, action) VALUES (?,?,'know')", (wid, old))
    c.commit()

    assert client.get("/api/stats?days=7").get_json()["period"]["know"] == 2
    today = client.get("/api/stats?days=7").get_json()["period"]
    assert today["active_days"] >= 1 and today["words_touched"] >= 1


def test_answer_shows_up_in_stats_at_once(client, words):
    """Главная жалоба: ответ дан, а в статистике его нет."""
    before = client.get("/api/stats?days=7").get_json()
    client.post("/api/answer", json={"word_id": words["draw"], "action": "know"})
    after = client.get("/api/stats?days=7").get_json()
    assert after["period"]["know"] == before["period"]["know"] + 1
    assert after["stats"]["know_today"] == before["stats"]["know_today"] + 1


def test_learned_endpoint_returns_everything(client, words):
    for name in ("draw", "hide", "write"):
        for _ in range(2):
            client.post("/api/answer", json={"word_id": words[name], "action": "know"})
    items = client.get("/api/learned").get_json()["items"]
    assert len(items) == 3
    assert all(w["learned_at"] for w in items)


def test_relearn_endpoint(client, words):
    wid = words["draw"]
    for _ in range(2):
        client.post("/api/answer", json={"word_id": wid, "action": "know"})
    assert len(client.get("/api/learned").get_json()["items"]) == 1

    r = client.post(f"/api/word/{wid}/relearn").get_json()
    assert r["ok"] and r["status"] == "learning" and r["reps"] == 0
    assert client.get("/api/learned").get_json()["items"] == []


def test_relearn_of_missing_word_is_an_honest_404(client):
    assert client.post("/api/word/999999/relearn").status_code == 404


def test_word_history_endpoint(client, words):
    wid = words["draw"]
    client.post("/api/answer", json={"word_id": wid, "action": "know"})
    h = client.get(f"/api/word/{wid}/history").get_json()
    assert h["word"] == "draw" and h["know_cnt"] == 1
    assert h["ru_read"], "в истории тоже нужно чтение русскими буквами"
    assert client.get("/api/word/999999/history").status_code == 404


def test_word_list_carries_history_columns(client, words):
    wid = words["draw"]
    client.post("/api/answer", json={"word_id": wid, "action": "know"})
    row = next(w for w in client.get("/api/words").get_json() if w["id"] == wid)
    for field in ("added_at", "first_shown_at", "last_shown_at",
                  "shown_cnt", "know_cnt", "again_cnt", "days_learning"):
        assert field in row, field
    assert row["shown_cnt"] == 1 and row["know_cnt"] == 1


def test_word_list_can_be_sorted_by_shows(client, words):
    rare, often = words["draw"], words["hide"]
    for _ in range(5):
        client.post("/api/answer", json={"word_id": often, "action": "skip"})
    client.post("/api/answer", json={"word_id": rare, "action": "skip"})
    first = client.get("/api/words?sort=shown").get_json()[0]
    assert first["id"] == often
    assert client.get("/api/words?sort=rare").get_json()[0]["shown_cnt"] == 0
