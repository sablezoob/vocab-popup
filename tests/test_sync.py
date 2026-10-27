# -*- coding: utf-8 -*-
"""Синхронизация с сервером — местная половина.

Обмен с MySQL проверяется отдельно (см. `test_sync_mysql.py`): для него
нужна живая база. Здесь — всё, что работает без сети и чаще всего ломается:
отметки времени, ключи строк, следы удалений и сдвиг «докуда дошли».
"""
import time

import pytest

import db
import sync


def ts(word_id=None, word=None):
    c = db.conn()
    if word is not None:
        return c.execute("SELECT updated_at FROM words WHERE word=?", (word,)).fetchone()[0]
    return c.execute("SELECT updated_at FROM srs WHERE word_id=?", (word_id,)).fetchone()[0]


def test_new_word_gets_a_timestamp(fresh_db):
    fresh_db.add_word("draw", translation="рисовать", ipa="/drɔː/")
    assert ts(word="draw"), "без отметки правки строка никуда не уедет"


def test_edit_moves_the_timestamp(fresh_db, words):
    before = ts(word="draw")
    time.sleep(1.1)                       # отметка с точностью до секунды
    fresh_db.conn().execute("UPDATE words SET note='правка' WHERE word='draw'")
    fresh_db.conn().commit()
    assert ts(word="draw") > before


def test_answer_moves_the_srs_timestamp(fresh_db, words):
    import srs
    before = ts(word_id=words["draw"])
    time.sleep(1.1)
    srs.grade(words["draw"], "know")
    assert ts(word_id=words["draw"]) > before, "ответ обязан уехать на сервер"


def test_timestamp_from_the_server_is_kept(fresh_db):
    """Строка, приехавшая с сервера, приносит свою отметку.

    Если затереть её текущим временем, слово будет выглядеть свежее
    серверного и при следующей сверке поедет обратно — и так по кругу.
    """
    theirs = "2020-05-05T05:05:05+00:00"
    c = fresh_db.conn()
    c.execute("INSERT INTO words(word, created_at, updated_at) VALUES ('ghost', ?, ?)",
              (theirs, theirs))
    c.commit()
    assert ts(word="ghost") == theirs

    c.execute("UPDATE words SET translation='оттуда', updated_at=? WHERE word='ghost'",
              ("2021-01-01T00:00:00+00:00",))
    c.commit()
    assert ts(word="ghost") == "2021-01-01T00:00:00+00:00"


def test_every_event_gets_its_own_key(fresh_db, words):
    for _ in range(5):
        db.log_event(words["draw"], "skip")
    uids = [r["uid"] for r in fresh_db.conn().execute("SELECT uid FROM events")]
    assert len(uids) == 5
    assert all(uids) and len(set(uids)) == 5, "номера строк на разных машинах совпадут"


def test_deleted_word_leaves_a_trace(fresh_db, words):
    """Без следа сервер вернул бы слово при следующей же сверке."""
    fresh_db.conn().execute("DELETE FROM words WHERE word='draw'")
    fresh_db.conn().commit()
    row = fresh_db.conn().execute("SELECT word FROM deletions").fetchone()
    assert row and row["word"] == "draw"


def test_mark_moves_forward_only(fresh_db):
    sync.mark("words_pull", "2026-01-01T00:00:00+00:00")
    sync.advance("words_pull", "2025-01-01T00:00:00+00:00")
    assert sync.mark("words_pull") == "2026-01-01T00:00:00+00:00"
    sync.advance("words_pull", "2026-02-02T00:00:00+00:00")
    assert sync.mark("words_pull") == "2026-02-02T00:00:00+00:00"


def test_a_date_from_the_future_does_not_stop_the_exchange(fresh_db):
    """Сбитые часы на другой машине или правка руками в DBeaver.

    Отметка, уехавшая в 2099 год, навсегда остановила бы приём: всё
    последующее считалось бы «старее уже полученного».
    """
    sync.advance("words_pull", "2099-01-01T00:00:00+00:00")
    got = sync.mark("words_pull")
    assert got < "2030-01-01", f"отметка ушла в будущее: {got}"
    assert got <= db.now_iso()


def test_watermark_is_shifted_back_for_clock_skew(fresh_db):
    """Переслать лишнее дешевле, чем потерять: лишнее отсеется по ключу."""
    sync.mark("events_push", "2026-06-01T12:00:00+00:00")
    assert sync._since_value("events_push") == "2026-06-01T11:00:00+00:00"


def test_empty_watermark_means_everything(fresh_db):
    assert sync._since_value("events_push") == sync.EPOCH


def test_broken_watermark_does_not_block_sync(fresh_db):
    sync.mark("events_push", "не дата")
    assert sync._since_value("events_push") == sync.EPOCH


def test_sentence_key_does_not_depend_on_row_numbers(fresh_db):
    a = sync.sent_key("Draw", "He has drawn a map.")
    b = sync.sent_key("draw", "He has drawn a map.")
    c = sync.sent_key("draw", "He has drawn a cat.")
    assert a == b, "слово сверяется без учёта регистра"
    assert a != c
    assert len(a) == 40


def test_pulled_word_gets_a_schedule(fresh_db):
    """Слово без строки в srs лежит в словаре и молчит: ни показа, ни фильтров."""
    c = fresh_db.conn()
    c.execute("""INSERT INTO words(word, translation, created_at, updated_at)
                 VALUES ('ghost','призрак','2026-01-01T00:00:00+00:00',
                         '2026-01-01T00:00:00+00:00')""")
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM srs").fetchone()[0] == 0
    assert sync._ensure_srs_rows(c) == 1
    assert c.execute("SELECT status FROM srs").fetchone()[0] == "new"


def test_schedule_is_not_created_twice(fresh_db, words):
    before = fresh_db.conn().execute("SELECT COUNT(*) FROM srs").fetchone()[0]
    assert sync._ensure_srs_rows(fresh_db.conn()) == 0
    assert fresh_db.conn().execute("SELECT COUNT(*) FROM srs").fetchone()[0] == before


def test_secrets_never_leave_the_machine(fresh_db):
    """На сервер уезжают только настройки учёбы.

    Ключ нейросети, пароль от самого сервера и положение окна — личное
    дело этой машины, и отправлять их незачем.
    """
    forbidden = {"ai_key", "sync_password", "sync_user", "sync_ssh_key",
                 "sync_host", "sync_db", "corner", "margin_px", "paused",
                 "pause_until", "card_scale"}
    assert not (set(sync.SHARED_SETTINGS) & forbidden)


def test_sync_is_off_until_switched_on(fresh_db):
    fresh_db.put("sync_enabled", "0")
    assert sync.due() is False
    assert sync.tick() is None


def test_schedule_waits_for_the_interval(fresh_db, monkeypatch):
    import remote
    monkeypatch.setattr(remote, "is_configured", lambda: True)
    fresh_db.put("sync_enabled", "1")
    fresh_db.put("sync_every_min", "15")
    fresh_db.put("sync_at", "")
    assert sync.due(), "ни разу не сверялись — надо"

    fresh_db.put("sync_at", db.now_iso())
    assert not sync.due(), "только что сверялись"

    from datetime import datetime, timedelta, timezone
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
    fresh_db.put("sync_at", long_ago)
    assert sync.due()


def test_status_counts_what_is_waiting(fresh_db, words, monkeypatch):
    import remote
    monkeypatch.setattr(remote, "is_configured", lambda: True)
    monkeypatch.setattr(remote, "port_open", lambda *a, **k: False)
    db.log_event(words["draw"], "skip")
    st = sync.status()
    assert st["pending"]["words"] >= 1 and st["pending"]["events"] >= 1
    assert st["tunnel"] is False
