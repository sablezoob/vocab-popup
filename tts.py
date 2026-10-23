# -*- coding: utf-8 -*-
"""Озвучка слов и примеров нейроголосом Microsoft.

Файл озвучивается один раз и кладётся в кеш: дальше воспроизведение идёт
офлайн и мгновенно. Синтез занимает секунду-две, поэтому его нельзя делать
в потоке интерфейса — карточка бы подвисала.
"""
import base64
import hashlib
import logging
import os
import subprocess
import sys
import threading

import db

log = logging.getLogger("vocab.tts")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "data", "audio")
VOICES = {
    "uk-female": "en-GB-SoniaNeural",
    "uk-male": "en-GB-RyanNeural",
    "us-female": "en-US-AriaNeural",
    "us-male": "en-US-GuyNeural",
}


def is_enabled():
    return db.get("tts_enabled", "0") == "1"


def voice():
    return VOICES.get(db.get("tts_voice", "uk-female"), VOICES["uk-female"])


def _path(text):
    key = hashlib.sha1(f"{voice()}|{text}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{key}.mp3")


def _synthesize(text, path):
    """Скачивает озвучку. Требует сети, но только при первом обращении.

    Сорвавшийся синтез не должен оставлять в кеше пустой файл: дальше
    `say` видел бы его и считал слово уже озвученным, а звука не было бы
    никогда — и починить это можно было бы только руками, через папку.
    """
    import asyncio

    os.makedirs(os.path.dirname(path), exist_ok=True)

    import edge_tts

    async def go():
        comm = edge_tts.Communicate(text, voice())
        await comm.save(path)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(go())
    except BaseException:
        _drop(path)
        raise
    if not _ready(path):
        _drop(path)
        raise RuntimeError("голосовой сервис вернул пустой файл")


def _ready(path):
    """Файл пригоден к воспроизведению: существует и не пуст."""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _drop(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        log.exception("Не удалось убрать испорченный файл озвучки %s", path)


# Проигрыватель WPF. Прежний вариант на COM-объекте WMPlayer.OCX молча
# не работал: состояние навсегда застревало на 9 («переход»), позиция
# оставалась нулевой, звука не было и ошибки тоже. MediaPlayer из
# PresentationCore проигрывает mp3 и честно сообщает длительность.
#
# Длительность важна: фиксированная пауза обрезала бы пример на середине,
# а слишком длинная держала бы лишний процесс. NaturalDuration появляется
# не сразу после Open, поэтому её дожидаемся.
PS_PLAY = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationCore
$player = New-Object System.Windows.Media.MediaPlayer
$player.Open([uri]$env:VOCAB_TTS_FILE)
for ($i = 0; $i -lt 60 -and -not $player.NaturalDuration.HasTimeSpan; $i++) {
    Start-Sleep -Milliseconds 50
}
$player.Play()
$ms = 5000
if ($player.NaturalDuration.HasTimeSpan) {
    $ms = [int]$player.NaturalDuration.TimeSpan.TotalMilliseconds + 400
}
Start-Sleep -Milliseconds $ms
$player.Close()
"""


def _play(path, wait=False):
    """Проигрывает файл скрытым процессом.

    Свой аудиовыход тянуть в приложение ради одной кнопки незачем, тем более
    что PySide6 здесь стоит без модуля QtMultimedia. С `wait=True` дожидается
    конца — так самопроверка может сказать, что звук правда прозвучал.
    """
    if sys.platform != "win32":
        return False
    # Путь передаём переменной окружения, а не внутрь команды: так его
    # не нужно экранировать и строка команды не зависит от имени файла.
    env = dict(os.environ, VOCAB_TTS_FILE=path)
    encoded = base64.b64encode(PS_PLAY.encode("utf-16-le")).decode("ascii")
    cmd = ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden",
           "-EncodedCommand", encoded]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if wait:
        r = subprocess.run(cmd, env=env, creationflags=flags,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60)
        return r.returncode == 0
    subprocess.Popen(cmd, env=env, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def say(text):
    """Озвучить текст. Возвращает сразу — работа идёт в фоне."""
    text = (text or "").strip()
    if not text or not is_enabled():
        return False

    def worker():
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            path = _path(text)
            if not _ready(path):
                _synthesize(text, path)
            _play(path)
        except Exception:
            log.exception("Не удалось озвучить: %s", text[:40])

    threading.Thread(target=worker, daemon=True).start()
    return True


def check(text="Hello. This is how the voice sounds."):
    """Проверка озвучки целиком: синтез, затем воспроизведение.

    Нужна потому, что `say` работает в фоне и молчит об ошибках — именно так
    озвучка и оказалась сломанной незаметно: проигрыватель не играл, а потом
    голосовой сервис стал отвечать отказом. Здесь обе беды видны сразу.
    """
    if not is_enabled():
        return False, "озвучка выключена в настройках"
    path = _path(text)
    fresh = not _ready(path)
    if fresh:
        try:
            _synthesize(text, path)
        except Exception as e:
            log.exception("Самопроверка озвучки: синтез не удался")
            return False, f"синтез не удался: {type(e).__name__}: {str(e)[:160]}"
    try:
        played = _play(path, wait=True)
    except subprocess.TimeoutExpired:
        return False, "проигрыватель не ответил за минуту"
    except Exception as e:
        log.exception("Самопроверка озвучки: воспроизведение не удалось")
        return False, f"воспроизведение не удалось: {type(e).__name__}"
    if not played:
        return False, "проигрыватель завершился с ошибкой"
    return True, {"voice": voice(), "bytes": os.path.getsize(path),
                  "synthesized": fresh}


def cache_size():
    if not os.path.isdir(CACHE_DIR):
        return 0, 0
    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".mp3")]
    total = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in files)
    return len(files), total


def clear_cache():
    n = 0
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".mp3"):
                os.remove(os.path.join(CACHE_DIR, f))
                n += 1
    return n
