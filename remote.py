# -*- coding: utf-8 -*-
"""Связь с удалённой базой: SSH-туннель и подключение к MySQL.

Сервер пускает только через туннель — порт MySQL наружу закрыт. Туннель
обычно поднимает DBeaver, но полагаться на это нельзя: закрыли DBeaver, и
синхронизация молча перестала работать. Поэтому при необходимости поднимаем
свой, отдельным процессом, и следим, что он жив.

Пароль и ключ живут в настройках базы, как и ключ нейросети: в репозиторий
`data/vocab.db` не попадает, а в коде секретов нет.
"""
import logging
import os
import socket
import subprocess
import threading
import time

import db

log = logging.getLogger("vocab.remote")

CONNECT_TIMEOUT = 10
TUNNEL_WAIT = 20          # столько ждём, пока поднимется свой туннель
_local = threading.local()
_tunnel = {"proc": None}
_lock = threading.Lock()


def settings():
    return {
        "host": db.get("sync_host", "127.0.0.1"),
        "port": db.get_int("sync_port", 13306),
        "database": db.get("sync_db", ""),
        "user": db.get("sync_user", ""),
        "password": db.get("sync_password", ""),
        "ssh_host": db.get("sync_ssh_host", ""),
        "ssh_user": db.get("sync_ssh_user", ""),
        "ssh_key": db.get("sync_ssh_key", ""),
    }


def is_configured():
    s = settings()
    return bool(s["database"] and s["user"] and s["password"])


def port_open(host=None, port=None, timeout=2.0):
    s = settings()
    host, port = host or s["host"], port or s["port"]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------- туннель ----------

def tunnel_alive():
    p = _tunnel["proc"]
    return p is not None and p.poll() is None


def start_tunnel():
    """Поднимает свой SSH-туннель, если порт никто не слушает.

    Чужой туннель (от DBeaver) не трогаем — два процесса на одном порту
    не уживутся, а закрывать чужой мы не вправе.
    """
    s = settings()
    if port_open():
        return True
    if not (s["ssh_host"] and s["ssh_user"] and s["ssh_key"]):
        log.warning("Туннель не настроен: нет адреса, пользователя или ключа")
        return False
    if not os.path.exists(s["ssh_key"]):
        log.warning("Файл ключа не найден: %s", s["ssh_key"])
        return False

    with _lock:
        if port_open():
            return True
        if tunnel_alive():
            _tunnel["proc"].kill()
        cmd = ["ssh", "-i", s["ssh_key"], "-N",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=30",
               "-o", "ServerAliveCountMax=3",
               "-L", f"{s['port']}:127.0.0.1:3306",
               f"{s['ssh_user']}@{s['ssh_host']}"]
        log.info("Поднимаю туннель: %s", " ".join(cmd[-3:]))
        _tunnel["proc"] = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    deadline = time.time() + TUNNEL_WAIT
    while time.time() < deadline:
        if port_open():
            return True
        if not tunnel_alive():
            err = ""
            try:
                err = (_tunnel["proc"].stderr.read() or b"").decode("utf-8", "replace")
            except Exception:
                pass
            log.warning("Туннель упал сразу: %s", err.strip()[:300])
            return False
        time.sleep(0.4)
    log.warning("Туннель не открыл порт за %s секунд", TUNNEL_WAIT)
    return False


def stop_tunnel():
    with _lock:
        p = _tunnel["proc"]
        _tunnel["proc"] = None
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


# ---------- подключение ----------

def connect(fresh=False):
    """Подключение к MySQL, своё на поток.

    Сервер закрывает простаивающие соединения, а приложение живёт сутками,
    поэтому перед выдачей соединение проверяется и при надобности поднимается
    заново — иначе первая же синхронизация после паузы падала бы.
    """
    import pymysql

    if not is_configured():
        raise RuntimeError("удалённая база не настроена")
    c = getattr(_local, "conn", None)
    if c is not None and not fresh:
        try:
            c.ping(reconnect=True)
            return c
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            _local.conn = None

    if not port_open() and not start_tunnel():
        raise ConnectionError("нет туннеля к серверу")

    s = settings()
    c = pymysql.connect(host=s["host"], port=s["port"], user=s["user"],
                        password=s["password"], database=s["database"],
                        charset="utf8mb4", autocommit=False,
                        connect_timeout=CONNECT_TIMEOUT,
                        cursorclass=pymysql.cursors.DictCursor)
    _local.conn = c
    return c


def close():
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _local.conn = None


# ---------- схема на сервере ----------
#
# Номера строк у каждой машины свои, поэтому на сервере их нет вовсе:
# ключом служит само слово, а у дописываемых таблиц — случайный uid.
# Время хранится строкой в том же виде, что и локально: так оно читается
# глазами в DBeaver и сравнивается без преобразований.

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS words (
        word        VARCHAR(190) NOT NULL PRIMARY KEY,
        ipa         VARCHAR(255) DEFAULT '',
        translation TEXT,
        example_en  TEXT,
        example_ru  TEXT,
        level       VARCHAR(16)  DEFAULT '',
        tags        VARCHAR(255) DEFAULT '',
        note        TEXT,
        enriched    TINYINT      DEFAULT 0,
        created_at  VARCHAR(32)  NOT NULL,
        v2          VARCHAR(190) DEFAULT '',
        v3          VARCHAR(190) DEFAULT '',
        ipa2        VARCHAR(255) DEFAULT '',
        ipa3        VARCHAR(255) DEFAULT '',
        ru_read     VARCHAR(255) DEFAULT '',
        updated_at  VARCHAR(32)  NOT NULL,
        KEY ix_words_updated (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS srs (
        word           VARCHAR(190) NOT NULL PRIMARY KEY,
        ease           DOUBLE      DEFAULT 2.5,
        interval_min   DOUBLE      DEFAULT 0,
        due_at         VARCHAR(32) DEFAULT '',
        reps           INT         DEFAULT 0,
        lapses         INT         DEFAULT 0,
        status         VARCHAR(16) DEFAULT 'new',
        learned_at     VARCHAR(32) DEFAULT '',
        first_shown_at VARCHAR(32) DEFAULT '',
        priority_at    VARCHAR(32) DEFAULT '',
        updated_at     VARCHAR(32) NOT NULL,
        KEY ix_srs_updated (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS events (
        uid        CHAR(32)     NOT NULL PRIMARY KEY,
        word       VARCHAR(190) NOT NULL,
        shown_at   VARCHAR(32)  NOT NULL,
        action     VARCHAR(16)  NOT NULL,
        ms_visible INT          DEFAULT 0,
        source     VARCHAR(16)  DEFAULT 'popup',
        KEY ix_events_shown (shown_at),
        KEY ix_events_word (word)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS sentences (
        k          CHAR(40)     NOT NULL PRIMARY KEY,
        word       VARCHAR(190) NOT NULL,
        text_en    TEXT         NOT NULL,
        text_ru    TEXT,
        source     VARCHAR(16)  DEFAULT 'ai',
        shown      INT          DEFAULT 0,
        created_at VARCHAR(32)  NOT NULL,
        KEY ix_sent_word (word)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS sessions (
        uid         CHAR(32)    NOT NULL PRIMARY KEY,
        started_at  VARCHAR(32) NOT NULL,
        finished_at VARCHAR(32) NOT NULL,
        mode        VARCHAR(16) DEFAULT '',
        deck        VARCHAR(64) DEFAULT '',
        total       INT DEFAULT 0,
        right_cnt   INT DEFAULT 0,
        wrong_cnt   INT DEFAULT 0,
        skipped     INT DEFAULT 0,
        seconds     INT DEFAULT 0,
        KEY ix_sessions_fin (finished_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS settings (
        k          VARCHAR(64) NOT NULL PRIMARY KEY,
        value      TEXT,
        updated_at VARCHAR(32) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS deletions (
        word       VARCHAR(190) NOT NULL PRIMARY KEY,
        deleted_at VARCHAR(32)  NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
]


def ensure_schema(conn=None):
    c = conn or connect()
    with c.cursor() as cur:
        for sql in SCHEMA:
            cur.execute(sql)
    c.commit()
    return c


def check():
    """Проверка связи для кнопки в дашборде: внятный ответ вместо тишины."""
    if not is_configured():
        return False, "не заполнены адрес, пользователь или пароль"
    try:
        c = connect(fresh=True)
        ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("SELECT VERSION() v, DATABASE() d")
            row = cur.fetchone()
            counts = {}
            for t in ("words", "events", "srs", "sentences", "sessions"):
                cur.execute(f"SELECT COUNT(*) n FROM {t}")
                counts[t] = cur.fetchone()["n"]
        return True, {"server": row["v"], "database": row["d"],
                      "tunnel": "свой" if tunnel_alive() else "сторонний",
                      "rows": counts}
    except Exception as e:
        log.exception("Проверка связи с сервером не прошла")
        return False, f"{type(e).__name__}: {str(e)[:200]}"
