# -*- coding: utf-8 -*-
"""Обращения к нейросети: примеры предложений, новые слова, разбор незнакомых.

Ключ и модель живут в настройках (в базе), а не в коде — база в репозиторий
не попадает. Модель отвечает десятками секунд, поэтому вызывать её из потока
интерфейса нельзя: всё это крутится в фоновом воркере (aiworker.py).
"""
import json
import logging
import random
import re

import db

log = logging.getLogger("vocab.ai")

TIMEOUT = 180          # модель думает долго; лучше подождать, чем дёргать повторно
MAX_TOKENS = 3000


class AiError(RuntimeError):
    pass


class RateLimited(AiError):
    """Сервис просит подождать: параллельные запросы на бесплатном тарифе
    упираются в 429, поэтому воркер работает в один поток и делает паузу."""


def is_enabled():
    return db.get("ai_enabled", "0") == "1" and bool((db.get("ai_key", "") or "").strip())


def _client():
    key = (db.get("ai_key", "") or "").strip()
    if not key:
        raise AiError("ключ не задан")
    try:
        from openai import OpenAI
    except ImportError as e:
        raise AiError("не установлен пакет openai") from e
    return OpenAI(base_url=db.get("ai_base_url") or "https://integrate.api.nvidia.com/v1",
                  api_key=key, timeout=TIMEOUT)


def _ask(prompt, max_tokens=MAX_TOKENS, temperature=0.8):
    """Один запрос к модели. Возвращает текст ответа."""
    client = _client()
    try:
        return _call(client, prompt, max_tokens, temperature)
    except Exception as e:
        if type(e).__name__ == "RateLimitError" or "429" in str(e):
            raise RateLimited("сервис ограничил частоту запросов") from e
        raise


def _call(client, prompt, max_tokens, temperature):
    r = client.chat.completions.create(
        model=db.get("ai_model") or "deepseek-ai/deepseek-v4.1-flash",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature, top_p=0.95, max_tokens=max_tokens,
        # рассуждения нам не нужны: они втрое удлиняют и без того долгий ответ
        extra_body={"chat_template_kwargs": {"thinking": False}},
        stream=False, timeout=TIMEOUT)
    return (r.choices[0].message.content or "").strip()


def _json(text):
    """Достаёт JSON из ответа: модель любит обернуть его в ```json."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)      # вдруг вокруг JSON осталась болтовня
        if not m:
            raise AiError(f"ответ не разобран как JSON: {t[:200]}")
        return json.loads(m.group(0))


# ---------------------------------------------------------------- предложения
def make_sentences(word, translation, count=3, forms=("", ""), grammar=None, level=None):
    """Свежие примеры для слова. Возвращает список пар (en, ru)."""
    grammar = grammar or db.get("ai_grammar") or "Present Perfect"
    level = level or db.get("ai_level") or "A2-B1"
    v2, v3 = forms
    hint = f' Its forms: V2 "{v2}", V3 "{v3}".' if v2 else ""
    prompt = f"""Return ONLY valid JSON, no markdown fences, no explanation:
{{"sentences":[{{"en":"...","ru":"..."}}]}}

Generate {count} DIFFERENT natural English sentences using the word "{word}"
(meaning: {translation}).{hint}
Requirements:
- grammar: {grammar}
- level: {level}
- each sentence 5-11 words, everyday situations, no rare vocabulary
- sentences must differ in subject and situation from each other
- "ru" is an accurate natural Russian translation"""
    data = _json(_ask(prompt, max_tokens=1200))

    # Проверяем, что слово действительно попало в предложение. Искать целиком
    # нельзя: у неправильных глаголов корень меняется («hide» -> «hidden»),
    # поэтому сверяемся с началом каждой из форм.
    stems = {word.split()[0].lower()[:4]}
    for f in forms:
        if f:
            stems.add(f.split()[0].lower()[:4])
    stems = {s for s in stems if len(s) >= 3}

    out, dropped = [], []
    for s in data.get("sentences", []):
        en = (s.get("en") or "").strip()
        ru = (s.get("ru") or "").strip()
        if not en:
            continue
        if any(st in en.lower() for st in stems):
            out.append((en, ru))
        else:
            dropped.append(en)
    if not out:
        log.warning("Все примеры для «%s» отброшены: %s", word, dropped[:3])
        raise AiError(f"в примерах нет слова «{word}»")
    return out


def make_sentences_batch(items, count=3, grammar=None, level=None):
    """Примеры сразу для нескольких слов одним запросом.

    Так вчетверо меньше обращений: свободный тариф не даёт слать запросы
    параллельно (отвечает 429), а один ответ на пять слов занимает столько же
    времени, сколько ответ на одно.

    items — список словарей: word, translation, v2, v3.
    Возвращает {word: [(en, ru), ...]}.
    """
    grammar = grammar or db.get("ai_grammar") or "Present Perfect"
    level = level or db.get("ai_level") or "A2-B1"
    lines = []
    for it in items:
        forms = f' (forms: {it["v2"]}, {it["v3"]})' if it.get("v2") else ""
        lines.append(f'- "{it["word"]}" = {it["translation"]}{forms}')
    words_block = chr(10).join(lines)

    prompt = f"""Return ONLY valid JSON, no markdown fences, no explanation:
{{"items":[{{"word":"...","sentences":[{{"en":"...","ru":"..."}}]}}]}}

For EACH word below generate {count} DIFFERENT natural English sentences.
Words:
{words_block}

Requirements:
- grammar: {grammar}
- level: {level}
- each sentence 5-11 words, everyday situations, no rare vocabulary
- sentences for one word must differ in subject and situation
- "ru" is an accurate natural Russian translation
- keep "word" exactly as given"""
    data = _json(_ask(prompt, max_tokens=MAX_TOKENS))

    by_word = {}
    for it in items:
        by_word[it["word"].lower()] = it
    out = {}
    for entry in data.get("items", []):
        name = (entry.get("word") or "").strip()
        src = by_word.get(name.lower())
        if not src:
            continue
        stems = {name.lower()[:4]}
        for f in (src.get("v2"), src.get("v3")):
            if f:
                stems.add(f.split()[0].lower()[:4])
        stems = {x for x in stems if len(x) >= 3}
        pairs = []
        for sent in entry.get("sentences", []):
            en = (sent.get("en") or "").strip()
            ru = (sent.get("ru") or "").strip()
            if en and any(st in en.lower() for st in stems):
                pairs.append((en, ru))
        if pairs:
            out[src["word"]] = pairs
    if not out:
        raise AiError("пакет примеров пуст")
    return out


# ------------------------------------------------------------------ новые слова
def make_words(count=20, known=(), level=None, topic=""):
    """Новые слова с переводом, транскрипцией и формами глагола."""
    level = level or db.get("ai_level") or "A2-B1"
    # Чем длиннее список известных, тем реже модель повторяется. Просим с запасом:
    # часть слов всё равно окажется дублями и отсеется при вставке.
    sample = random.sample(list(known), min(120, len(known))) if known else []
    avoid = ", ".join(sample)
    topic_line = f"- topic: {topic}\n" if topic else ""
    prompt = f"""Return ONLY valid JSON, no markdown fences, no explanation:
{{"words":[{{"word":"...","translation":"...","ipa":"/.../","v2":"","v3":"",
"ipa2":"","ipa3":"","example_en":"...","example_ru":"..."}}]}}

Give {int(count * 1.4)} useful English words for a Russian learner.
{topic_line}- level: {level}
- "translation": short Russian translation (1-4 words)
- "ipa": British transcription in slashes, e.g. /əˈtʃiːv/
- for irregular verbs fill v2, v3 and their transcriptions ipa2, ipa3;
  for other words leave these four fields as empty strings
- "example_en": one short sentence with the word, "example_ru": its translation
- The learner ALREADY KNOWS these words, do not repeat any of them: {avoid}
- prefer common everyday words a learner meets in real life
- no proper nouns, no rare or archaic words"""
    data = _json(_ask(prompt, max_tokens=MAX_TOKENS, temperature=0.9))
    words = []
    for w in data.get("words", []):
        name = (w.get("word") or "").strip()
        tr = (w.get("translation") or "").strip()
        if not name or not tr:
            continue
        words.append({
            "word": name, "translation": tr,
            "ipa": (w.get("ipa") or "").strip(),
            "v2": (w.get("v2") or "").strip(), "v3": (w.get("v3") or "").strip(),
            "ipa2": (w.get("ipa2") or "").strip(), "ipa3": (w.get("ipa3") or "").strip(),
            "example_en": (w.get("example_en") or "").strip(),
            "example_ru": (w.get("example_ru") or "").strip(),
        })
    if not words:
        raise AiError("модель не вернула слов")
    return words


# ------------------------------------------------- разбор незнакомого слова
def explain(word, context=""):
    """Карточка для слова, отмеченного как незнакомое прямо в примере."""
    ctx = f'\nIt was met in the sentence: "{context}"' if context else ""
    prompt = f"""Return ONLY valid JSON, no markdown fences, no explanation:
{{"word":"...","translation":"...","ipa":"/.../","v2":"","v3":"","ipa2":"","ipa3":"",
"example_en":"...","example_ru":"..."}}

Make a vocabulary card for the English word "{word}" for a Russian learner.{ctx}
- "word": the dictionary form (infinitive for verbs, singular for nouns)
- "translation": short Russian translation (1-4 words); if the word is met in
  the sentence above, give the meaning it has there
- "ipa": British transcription in slashes
- if it is an irregular verb, fill v2, v3, ipa2, ipa3; otherwise leave them empty
- "example_en": one short simple sentence, "example_ru": its Russian translation"""
    w = _json(_ask(prompt, max_tokens=700, temperature=0.4))
    name = (w.get("word") or word).strip()
    tr = (w.get("translation") or "").strip()
    if not tr:
        raise AiError(f"перевод для «{word}» не получен")
    return {"word": name, "translation": tr,
            "ipa": (w.get("ipa") or "").strip(),
            "v2": (w.get("v2") or "").strip(), "v3": (w.get("v3") or "").strip(),
            "ipa2": (w.get("ipa2") or "").strip(), "ipa3": (w.get("ipa3") or "").strip(),
            "example_en": (w.get("example_en") or "").strip(),
            "example_ru": (w.get("example_ru") or "").strip()}


def check_connection():
    """Быстрая проверка ключа и модели для кнопки в настройках."""
    txt = _ask('Reply with only this JSON: {"ok":true}', max_tokens=40, temperature=0.1)
    return bool(_json(txt).get("ok"))
