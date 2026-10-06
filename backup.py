# -*- coding: utf-8 -*-
"""Резервные копии словаря и прогресса.

Зачем отдельный модуль, а не «скопируйте файл руками». База работает в режиме
WAL: свежие записи лежат не в `vocab.db`, а в соседнем `vocab.db-wal`, и тот
разрастается до мегабайтов. Копия одного `vocab.db` молча теряет последние
события — проверено на живой базе: 5808 записей против 5756 в такой копии.

Поэтому копия снимается встроенным механизмом SQLite `Connection.backup()`.
Он берёт согласованный снимок вместе с журналом и не мешает работе
приложения, даже если в этот момент показывается карточка.
"""
import glob
import logging
import os
import sqlite3
from datetime import datetime, timezone

import db

log = logging.getLogger("vocab.backup")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE_DIR, "data", "backups")
PREFIX = "vocab-"
SUFFIX = ".db"
EVERY_HOURS = 24


def _path_for(now=None):
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M")
    return os.path.join(DIR, f"{PREFIX}{stamp}{SUFFIX}")


def list_backups():
    """Копии от новых к старым: (путь, размер в байтах, время создания)."""
    out = []
    for p in glob.glob(os.path.join(DIR, f"{PREFIX}*{SUFFIX}")):
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append((p, st.st_size, datetime.fromtimestamp(st.st_mtime)))
    return sorted(out, key=lambda x: x[2], reverse=True)


def checkpoint():
    """Сбрасывает журнал в основной файл.

    Без этого `vocab.db-wal` растёт неограниченно: при базе в 1 МБ он доходил
    до 4 МБ. На сохранность это не влияет, но замедляет открытие и делает
    ручную копию файла ещё более неполной.
    """
    try:
        db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error:
        log.exception("Не удалось сбросить журнал")
        return False


def make(keep=None):
    """Снимает копию и удаляет лишние старые. Возвращает путь или None."""
    os.makedirs(DIR, exist_ok=True)
    path = _path_for()
    # Копия за ту же минуту уже есть — второй раз не пишем
    if os.path.exists(path):
        return path
    try:
        dest = sqlite3.connect(path)
        try:
            db.conn().backup(dest)
            # Копия наследует режим WAL и тянет за собой -wal и -shm. Для файла,
            # который лежит на полке и никем не пишется, журнал лишний: без него
            # копия — один самодостаточный файл, его можно просто перенести.
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest.close()
    except (sqlite3.Error, OSError):
        log.exception("Не удалось сделать резервную копию")
        if os.path.exists(path):
            os.remove(path)
        return None

    db.put("backup_at", db.now_iso())
    prune(keep)
    checkpoint()
    log.info("Резервная копия: %s", os.path.basename(path))
    return path


def prune(keep=None):
    """Оставляет только последние N копий. Удалённые не восстановить,
    поэтому запас по умолчанию недельный."""
    if keep is None:
        keep = db.get_int("backup_keep", 7)
    keep = max(1, keep)
    removed = 0
    for p, _size, _at in list_backups()[keep:]:
        try:
            os.remove(p)
            removed += 1
            # Хвосты журнала, если копия осталась от прежней версии
            for tail in ("-wal", "-shm"):
                if os.path.exists(p + tail):
                    os.remove(p + tail)
        except OSError:
            log.exception("Не удалось удалить старую копию %s", p)
    return removed


def due():
    """Пора ли делать копию. Считается от времени последней, а не от запуска:
    приложение могут перезапускать по десять раз в день."""
    if db.get("backup_enabled", "1") != "1":
        return False
    last = (db.get("backup_at", "") or "").strip()
    if not last:
        return True
    try:
        was = datetime.fromisoformat(last)
    except ValueError:
        return True          # отметка испорчена — лучше лишняя копия, чем ни одной
    gap = datetime.now(timezone.utc) - was
    return gap.total_seconds() >= EVERY_HOURS * 3600


def status():
    """Что показать в дашборде и в меню."""
    items = list_backups()
    return {
        "enabled": db.get("backup_enabled", "1") == "1",
        "keep": db.get_int("backup_keep", 7),
        "count": len(items),
        "dir": DIR,
        "last_at": items[0][2].strftime("%Y-%m-%d %H:%M") if items else "",
        "last_size": items[0][1] if items else 0,
        "items": [{"name": os.path.basename(p), "size": s,
                   "at": at.strftime("%Y-%m-%d %H:%M")} for p, s, at in items],
    }


def verify(path):
    """Проверяет, что копия открывается и содержит те же слова и события.

    Копия, которую никто не открывал, — это обещание, а не страховка.
    """
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "файл повреждён"
            got = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("words", "events", "srs")}
        finally:
            c.close()
    except sqlite3.Error as e:
        return False, str(e)
    live = {t: db.conn().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("words", "events", "srs")}
    # События могли добавиться уже после копии — это нормально, потери нет
    if got["words"] != live["words"] or got["events"] > live["events"]:
        return False, f"расхождение: в копии {got}, в базе {live}"
    return True, got


def tick():
    """Вызывается планировщиком. Делает копию, если подошёл срок."""
    if not due():
        return None
    return make()


if __name__ == "__main__":
    import sys
    p = make()
    if not p:
        print("копия не создана — смотрите data/app.log")
        sys.exit(1)
    ok, info = verify(p)
    print(f"{os.path.basename(p)}  {os.path.getsize(p)} байт")
    print("проверка:", "в порядке" if ok else f"ПРОБЛЕМА: {info}", info if ok else "")
    print("всего копий:", len(list_backups()))
