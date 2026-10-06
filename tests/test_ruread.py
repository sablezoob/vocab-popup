# -*- coding: utf-8 -*-
"""Чтение русскими буквами.

Транскрипция IPA точна, но её сперва надо выучить. Русская запись нужна,
чтобы слово можно было произнести сразу, — и ошибается она заметнее всего
на «р», которой в британском произношении нет, а в написании слова есть.
"""
import ruread

# Проверенные вручную пары: слово, транскрипция, ожидаемое чтение без ударения
KNOWN = [
    # обычные слова
    ("curious", "/ˈkjʊəriəs/", "кьюриэс"),
    ("confident", "/ˈkɒnfɪdənt/", "конфидэнт"),
    ("thousand", "/ˈθaʊznd/", "саузэнд"),
    ("species", "/ˈspiːʃiːz/", "спишиз"),
    ("healthy", "/ˈhelθi/", "хэлси"),
    ("this", "/ðɪs/", "зис"),
    # «w» — это «у», а не «в»: губы не касаются зубов
    ("wait", "/weɪt/", "уэйт"),
    ("work", "/wɜːk/", "уёрк"),
    # мягкость даёт только звук «j», долгая «ɜː» её не даёт
    ("queue", "/kjuː/", "кью"),
    ("music", "/ˈmjuːzɪk/", "мьюзик"),
    ("bird", "/bɜːd/", "бёрд"),
    # «ŋ» перед k и g — обычная «н»
    ("think", "/θɪŋk/", "синк"),
    ("thing", "/θɪŋ/", "синг"),
    # слоговые согласные: гласной в транскрипции нет, читать без неё нельзя
    ("often", "/ˈɒfn/", "офэн"),
    ("little", "/ˈlɪtl/", "литэл"),
]

# Слова, в которых британское произношение «съело» r, а чтению она нужна
RHOTIC = [
    ("car", "/kɑː/", "кар"),
    ("teacher", "/ˈtiːtʃə/", "тичэр"),
    ("injured", "/ˈɪndʒəd/", "инджэрд"),
    ("rare", "/reə/", "рэар"),
    # «р» достаётся последней гласной, а не первой
    ("water", "/ˈwɔːtə/", "уотэр"),
    # ...а здесь, наоборот, первой: r стоит в начале слова
    ("artificial intelligence", "/ˌɑːtɪˈfɪʃl ɪnˈtelɪdʒəns/", "артифишл интэлиджэнс"),
    # две немые r — по одной на каждую гласную
    ("forward", "/ˈfɔːwəd/", "форуэрд"),
    # r звучит и дважды не считается
    ("frustrated", "/frʌˈstreɪtɪd/", "фрастрэйтид"),
    ("curious", "/ˈkjʊəriəs/", "кьюриэс"),
    # после «э» идёт ещё гласная — значит, слог не последний и «р» не нужна
    ("salary", "/ˈsæləri/", "сэлэри"),
    # в написании r нет вовсе
    ("data", "/ˈdeɪtə/", "дэйтэ"),
]


def plain(word, ipa):
    return ruread.from_ipa(ipa, word).replace(ruread.STRESS, "")


def test_known_words():
    wrong = {w: (plain(w, ipa), want) for w, ipa, want in KNOWN
             if plain(w, ipa) != want}
    assert not wrong, f"чтение разошлось: {wrong}"


def test_swallowed_r_returns_to_the_right_vowel():
    wrong = {w: (plain(w, ipa), want) for w, ipa, want in RHOTIC
             if plain(w, ipa) != want}
    assert not wrong, f"«р» встала не туда: {wrong}"


def test_stress_marks_the_right_vowel():
    assert ruread.from_ipa("/ˈɒfn/", "often") == "о" + ruread.STRESS + "фэн"
    assert ruread.from_ipa("/frʌˈstreɪtɪd/", "frustrated").count(ruread.STRESS) == 1


def test_single_syllable_has_no_stress():
    """Ударять не во что — знак только мешал бы читать."""
    for word, ipa in (("car", "/kɑː/"), ("this", "/ðɪs/"), ("queue", "/kjuː/")):
        assert ruread.STRESS not in ruread.from_ipa(ipa, word), word


def test_nothing_to_read_gives_empty_string():
    assert ruread.from_ipa("", "word") == ""
    assert ruread.from_ipa("//", "word") == ""


def test_handwritten_reading_wins(fresh_db):
    """У записи из урока может быть своя причина — пересчёт её не трогает."""
    fresh_db.add_word("always", translation="всегда", ipa="/ˈɔːlweɪz/",
                      ru_read="олвэйз")
    row = fresh_db.conn().execute("SELECT * FROM words WHERE word='always'").fetchone()
    assert ruread.show(row) == "олвэйз"


def test_computed_when_nothing_written_by_hand(fresh_db):
    fresh_db.add_word("always", translation="всегда", ipa="/ˈɔːlweɪz/")
    row = fresh_db.conn().execute("SELECT * FROM words WHERE word='always'").fetchone()
    assert ruread.show(row).replace(ruread.STRESS, "") == "олуэйз"


def test_whole_dictionary_converts_cleanly():
    """Каждое слово всех колод должно читаться: без латиницы, не пустым.

    Непереведённый знак IPA молча выпадает из результата, и заметить это
    можно только на слове, которое сам не проверял.
    """
    import io
    import os
    import seed
    from webapp import parse_line

    broken = []
    for filename in seed.DECKS:
        path = os.path.join(seed.SEEDS, filename)
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = parse_line(line)
            if not p or not p["ipa"]:
                continue
            got = ruread.from_ipa(p["ipa"], p["word"])
            if not got or any(c.isascii() and c.isalpha() for c in got):
                broken.append((p["word"], p["ipa"], got))
    assert not broken, f"не читается: {broken[:10]}"
