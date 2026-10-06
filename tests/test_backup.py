# -*- coding: utf-8 -*-
"""Резервные копии.

Проверяется ровно то, ради чего модуль написан: копия должна содержать
данные, которых в простой копии файла `vocab.db` нет. База работает в режиме
WAL, свежие записи лежат в соседнем журнале, и `shutil.copy` их теряет молча.
"""
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import backup
import db


@pytest.fixture()
def box(fresh_db, tmp_path, monkeypatch):
    """Папка копий своя на каждый тест, рабочую не трогаем."""
    monkeypatch.setattr(backup, "DIR", str(tmp_path / "backups"))
    return fresh_db


def rows(path, table):
    """Сколько строк в файле. -1, если таблицы нет вовсе — у простой копии
    свежей базы не оказывается даже схемы: она целиком сидит в журнале."""
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return -1
    finally:
        c.close()


def test_backup_keeps_what_a_file_copy_loses(box, words):
    """Главная причина существования модуля."""
    for _ in range(40):
        db.log_event(words["draw"], "skip")
    live = db.conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]

    naive = str(box.DB_PATH) + ".naive"
    shutil.copy(box.DB_PATH, naive)

    path = backup.make()
    assert path and os.path.exists(path)
    assert rows(path, "events") == live, "копия обязана содержать все события"
    assert rows(naive, "events") < live, (
        "если простая копия вдруг полна, модуль больше не нужен — "
        "проверьте, не отключён ли WAL")


def test_backup_is_a_single_file(box, words):
    """Копия не должна тащить за собой -wal и -shm: её переносят как файл."""
    path = backup.make()
    assert not os.path.exists(path + "-wal")
    assert not os.path.exists(path + "-shm")


def test_verify_notices_a_broken_copy(box, words):
    path = backup.make()
    ok, _info = backup.verify(path)
    assert ok

    with open(path, "r+b") as f:        # портим середину файла
        f.seek(2048)
        f.write(b"\x00" * 512)
    ok, info = backup.verify(path)
    assert not ok, f"повреждённая копия прошла проверку: {info}"


def test_verify_notices_missing_words(box, words):
    path = backup.make()
    c = sqlite3.connect(path)
    c.execute("DELETE FROM words")
    c.commit()
    c.close()
    ok, _ = backup.verify(path)
    assert not ok


def test_only_the_newest_copies_are_kept(box, words):
    os.makedirs(backup.DIR, exist_ok=True)
    for i in range(10):
        p = os.path.join(backup.DIR, f"vocab-2026-01-{i + 1:02d}_10-00.db")
        open(p, "w").close()
        old = time.time() - (20 - i) * 86400
        os.utime(p, (old, old))

    assert backup.prune(keep=4) == 6
    left = [os.path.basename(p) for p, _s, _a in backup.list_backups()]
    assert len(left) == 4
    assert left[0] == "vocab-2026-01-10_10-00.db", "остаться должны самые свежие"


def test_schedule_waits_a_full_day(box, words):
    assert backup.due(), "копий ещё не было — надо сделать"
    backup.make()
    assert not backup.due(), "вторая копия в тот же час не нужна"

    long_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    db.put("backup_at", long_ago)
    assert backup.due()


def test_schedule_can_be_switched_off(box, words):
    db.put("backup_enabled", "0")
    assert not backup.due()
    assert backup.tick() is None


def test_broken_timestamp_does_not_block_backups(box, words):
    """Испорченная отметка не должна оставить пользователя без копий."""
    db.put("backup_at", "не дата")
    assert backup.due()


def test_checkpoint_shrinks_the_journal(box, words):
    for _ in range(200):
        db.log_event(words["draw"], "skip")
    assert backup.checkpoint()
    wal = str(box.DB_PATH) + "-wal"
    assert not os.path.exists(wal) or os.path.getsize(wal) < 100_000


def test_status_reports_what_the_dashboard_shows(box, words):
    empty = backup.status()
    assert empty["count"] == 0 and empty["last_at"] == ""

    backup.make()
    st = backup.status()
    assert st["count"] == 1 and st["last_at"] and st["last_size"] > 0
    assert st["items"][0]["name"].startswith("vocab-")


def test_web_makes_and_lists_backups(client, box, words):
    assert client.get("/api/backup").get_json()["count"] == 0
    r = client.post("/api/backup").get_json()
    assert r["ok"] and r["name"].startswith("vocab-")
    assert r["info"]["words"] > 0
    assert client.get("/api/backup").get_json()["count"] == 1
