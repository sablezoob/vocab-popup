# -*- coding: utf-8 -*-
"""Разбор пользовательского ввода и колод."""
import quiz
import verbforms
from webapp import parse_line


def test_import_minimal_line():
    assert parse_line("test")["word"] == "test"


def test_import_full_line():
    row = parse_line("w | перевод | /ipa/ | Example here. | Пример тут.")
    assert row["translation"] == "перевод"
    assert row["ipa"] == "/ipa/"
    assert row["example_en"] == "Example here."
    assert row["example_ru"] == "Пример тут."


def test_import_finds_transcription_in_any_position():
    row = parse_line("w | /ipa/ | перевод")
    assert row["ipa"] == "/ipa/" and row["translation"] == "перевод"


def test_import_rejects_garbage():
    assert parse_line("|||") is None
    assert parse_line("   ") is None


def test_import_keeps_dashes_inside_examples():
    """Тире служило разделителем и резало примеры пополам."""
    row = parse_line("w | перевод | He is well-known — and rich.")
    assert row["example_en"] == "He is well-known — and rich."


def test_verb_forms_dot_format():
    base, v2, v3, i1, i2, i3 = verbforms.parse("читать · read — read",
                                               "/riːd/ · /red/ · /red/")
    assert (base, v2, v3) == ("читать", "read", "read")
    assert (i1, i2, i3) == ("/riːd/", "/red/", "/red/")


def test_verb_forms_bracket_format():
    """Старый формат карточек: формы в скобках внутри перевода."""
    base, v2, v3, *_ = verbforms.parse("писать (wrote — written)", "/raɪt/")
    assert (base, v2, v3) == ("писать", "wrote", "written")


def test_verb_forms_plain_word_has_none():
    base, v2, v3, *_ = verbforms.parse("совет", "/ədˈvaɪs/")
    assert (base, v2, v3) == ("совет", "", "")


def test_typed_answer_forgives_case_and_spaces():
    assert quiz.check_typed("  DRAW  ", "draw")
    assert not quiz.check_typed("drew", "draw")


def test_quiz_options(words, fresh_db):
    row = fresh_db.conn().execute("SELECT * FROM words WHERE word='draw'").fetchone()
    options = quiz.translation_options(row)
    assert len(options) == quiz.OPTIONS
    assert row["translation"] in options
    assert len(set(options)) == quiz.OPTIONS, "варианты не должны повторяться"


def test_form_options_only_for_verbs(words, fresh_db):
    c = fresh_db.conn()
    verb = c.execute("SELECT * FROM words WHERE word='draw'").fetchone()
    plain = c.execute("SELECT * FROM words WHERE word='advice'").fetchone()
    assert verb["v3"] in quiz.form_options(verb)
    assert quiz.form_options(plain) == []


def test_quiz_needs_enough_words(fresh_db):
    """На двух словах четыре варианта не собрать — пустой список, не ошибка."""
    fresh_db.add_word("one", translation="один", ipa="/w/")
    fresh_db.add_word("two", translation="два", ipa="/t/")
    row = fresh_db.conn().execute("SELECT * FROM words WHERE word='one'").fetchone()
    assert quiz.translation_options(row) == []


def test_every_seed_line_is_usable():
    """Колоды лежат текстом, и опечатка в разделителе молча теряет слово:
    загрузчик просто пропускает строку, а в базе её потом не хватишься."""
    import io
    import os
    import seed

    for filename, (tags, _level, _ow) in seed.DECKS.items():
        path = os.path.join(seed.SEEDS, filename)
        assert os.path.exists(path), f"колода {filename} указана, но файла нет"
        seen = set()
        with io.open(path, encoding="utf-8") as f:
            for num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                where = f"{filename}:{num}"
                parsed = parse_line(line)
                assert parsed, f"{where}: строка не разобралась"
                assert parsed["translation"], f"{where}: нет перевода"
                low = parsed["word"].lower()
                assert low not in seen, f"{where}: слово {low} уже есть в колоде"
                seen.add(low)
        assert seen, f"колода {filename} пустая"
