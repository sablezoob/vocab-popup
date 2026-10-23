# -*- coding: utf-8 -*-
"""Озвучка.

Сеть и звуковую карту не трогаем: подменяем синтез и запуск проигрывателя.
Проверяется то, из-за чего озвучка однажды сломалась незаметно — пустой файл
в кеше после сорванного синтеза и отсутствие способа узнать, что звука нет.
"""
import os

import pytest

import tts


@pytest.fixture()
def cache(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", str(tmp_path / "audio"))
    fresh_db.put("tts_enabled", "1")
    return fresh_db


def fake_synth(monkeypatch, content=b"mp3-data"):
    """Подменяет скачивание: кладёт в файл что попросили."""
    calls = []

    def go(text, path):
        calls.append(text)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        if not content:
            tts._drop(path)
            raise RuntimeError("голосовой сервис вернул пустой файл")

    monkeypatch.setattr(tts, "_synthesize", go)
    return calls


def fake_play(monkeypatch, ok=True):
    played = []

    def go(path, wait=False):
        played.append(path)
        return ok

    monkeypatch.setattr(tts, "_play", go)
    return played


def test_off_by_default_means_silence(cache, monkeypatch):
    cache.put("tts_enabled", "0")
    played = fake_play(monkeypatch)
    assert tts.say("word") is False
    assert not played


def test_empty_file_is_not_left_in_cache(cache, monkeypatch):
    """Главная ловушка: пустой файл выглядит как готовая озвучка.

    После сорванного синтеза `say` видел бы его и считал слово озвученным,
    а звука не было бы никогда — и починить можно было бы только руками.
    """
    path = tts._path("word")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    assert os.path.exists(path)
    assert not tts._ready(path), "пустой файл не должен считаться готовым"

    calls = fake_synth(monkeypatch)
    fake_play(monkeypatch)
    tts.say("word")
    import time
    for _ in range(40):                 # синтез идёт в отдельном потоке
        if calls:
            break
        time.sleep(0.05)
    assert calls == ["word"], "пустой файл обязан вызвать повторный синтез"


def test_broken_synthesis_cleans_up_after_itself(cache, monkeypatch):
    fake_synth(monkeypatch, content=b"")
    path = tts._path("word")
    with pytest.raises(RuntimeError):
        tts._synthesize("word", path)
    assert not os.path.exists(path)


def test_check_reports_success(cache, monkeypatch):
    fake_synth(monkeypatch)
    played = fake_play(monkeypatch)
    ok, info = tts.check("hello")
    assert ok
    assert info["voice"] == "en-GB-SoniaNeural"
    assert info["synthesized"] is True and info["bytes"] > 0
    assert played, "проверка обязана действительно проиграть файл"


def test_check_uses_cache_on_second_run(cache, monkeypatch):
    fake_synth(monkeypatch)
    fake_play(monkeypatch)
    tts.check("hello")
    ok, info = tts.check("hello")
    assert ok and info["synthesized"] is False


def test_check_names_the_broken_half(cache, monkeypatch):
    """Ошибка должна называть виновника: синтез или проигрыватель."""
    def boom(text, path):
        raise OSError("нет сети")
    monkeypatch.setattr(tts, "_synthesize", boom)
    fake_play(monkeypatch)
    ok, err = tts.check("hello")
    assert not ok and "синтез" in err and "нет сети" in err

    fake_synth(monkeypatch)
    fake_play(monkeypatch, ok=False)
    ok, err = tts.check("hello2")
    assert not ok and "роигрыватель" in err


def test_check_says_when_switched_off(cache, monkeypatch):
    cache.put("tts_enabled", "0")
    ok, err = tts.check()
    assert not ok and "выключена" in err


def test_voice_falls_back_to_a_real_one(cache):
    cache.put("tts_voice", "чепуха")
    assert tts.voice() in tts.VOICES.values()


def test_player_command_does_not_depend_on_the_file_name(cache):
    """Путь уходит переменной окружения: кавычки и пробелы в имени
    не должны ломать команду."""
    assert "VOCAB_TTS_FILE" in tts.PS_PLAY
    assert "WMPlayer" not in tts.PS_PLAY, (
        "COM-проигрыватель молча не играл — возвращать его нельзя")


def test_web_button_reports_the_error(client, cache, monkeypatch):
    def boom(text, path):
        raise OSError("нет сети")
    monkeypatch.setattr(tts, "_synthesize", boom)
    fake_play(monkeypatch)
    r = client.post("/api/tts/test").get_json()
    assert r["ok"] is False and "нет сети" in r["error"]


def test_web_button_reports_success(client, cache, monkeypatch):
    fake_synth(monkeypatch)
    fake_play(monkeypatch)
    r = client.post("/api/tts/test").get_json()
    assert r["ok"] and r["info"]["bytes"] > 0
