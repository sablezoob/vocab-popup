# -*- coding: utf-8 -*-
"""Загрузка стартовых колод из seeds/*.txt. Повторный запуск безопасен —
существующие слова не дублируются, а лишь дополняются пустые поля."""
import io
import os
import sys

import db
import verbforms
from webapp import parse_line

BASE = os.path.dirname(os.path.abspath(__file__))
SEEDS = os.path.join(BASE, "seeds")

# файл -> (тег колоды, уровень, перезаписывать ли существующие карточки)
DECKS = {
    "seasons.txt": ("seasons", "A2", False),
    "present_perfect.txt": ("tense:present-perfect", "B1", False),
    "lesson_01.txt": ("lesson", "", False),
    "lesson_02.txt": ("lesson-02", "", False),
    # три формы дают более полную карточку, чем уже лежащая в базе, — перезаписываем
    "verbs_3forms.txt": ("verbs-3forms", "A2", True),
}


def load(filename, tags, level, overwrite=False):
    path = os.path.join(SEEDS, filename)
    if not os.path.exists(path):
        print(f"  пропуск: нет файла {filename}")
        return 0, 0
    created = updated = 0
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = parse_line(line)
            if not p:
                continue
            trans, ipa = p["translation"], p["ipa"]
            v2 = v3 = ipa2 = ipa3 = ""
            if "verbs-3forms" in tags:
                trans, v2, v3, ipa, ipa2, ipa3 = verbforms.parse(trans, ipa)
            wid, res = db.add_word(p["word"], ipa=ipa, translation=trans,
                                   example_en=p["example_en"], example_ru=p["example_ru"],
                                   level=level, tags=tags, overwrite=overwrite)
            if wid and v2:
                db.set_forms(wid, v2, v3, ipa2, ipa3)
            if res == "created":
                created += 1
            elif res == "updated":
                updated += 1
    print(f"  {filename}: добавлено {created}, дополнено {updated}  [{tags}]")
    return created, updated


def main():
    db.init()
    print("Загрузка стартовых колод...")
    total_c = total_u = 0
    for fn, (tags, level, overwrite) in DECKS.items():
        c, u = load(fn, tags, level, overwrite)
        total_c += c
        total_u += u
    print(f"Итого: добавлено {total_c}, дополнено {total_u}")
    import srs
    print("Состояние словаря:", srs.stats())


if __name__ == "__main__":
    sys.exit(main())
