# -*- coding: utf-8 -*-
"""Русское чтение слова: IPA переписывается кириллицей.

Зачем. Международная транскрипция точна, но её надо сперва выучить, а читать
слово хочется сразу. Русская запись приблизительна — часть английских звуков
в русском отсутствует, — но она сразу подсказывает, как открыть рот.

Что приблизительно и почему именно так:

* `θ` и `ð` (think, this) записаны как «с» и «з». Звука с языком между зубами
  в русском нет, и «с/з» ближе к нему, чем «ф/в» или «т/д».
* `w` записана как «у»: wait → «уэйт». Привычная «в» — другой звук, нижняя
  губа о зубы; английская `w` ближе к короткому «у».
* `æ` (cat) записана как «э». Настоящий звук — между «э» и «а», рот открыт шире.
* `ɜː` (bird, work) записана как «ё», но предыдущая согласная не смягчается.
* Британская транскрипция не произносит «р» на конце: car → /kɑː/. При чтении
  это сбивает, поэтому «р» возвращается, если она есть в написании слова.

Ударение отмечено знаком над гласной: «о́фэн». У односложных слов его нет —
там ударять всё равно некуда.
"""
import re

STRESS = "́"      # комбинируемый знак ударения, ставится ПОСЛЕ гласной
VOWELS = "аэеиоуыёюя"
CONSONANTS = "бвгджзйклмнпрстфхцчшщ"

# Звуки, из которых британское произношение убрало «р». Если в написании слова
# буква r есть, при чтении её возвращаем: car → «кар», а не «ка».
RHOTIC = {"ɑː": "ар", "ɔː": "ор", "ɜː": "ёр", "ə": "эр",
          "ɪə": "иэр", "eə": "эар", "ʊə": "уэр"}

# Разбор жадный, сначала самые длинные сочетания: иначе «tʃ» распадётся
# на «т» и «ш», а «juː» — на «й» и «у».
SOUNDS = [
    # йотированные: после согласной дают мягкость (queue → «кью»)
    ("juː", "ю"), ("jʊə", "ю"), ("jʊ", "ю"), ("jə", "е"), ("jɜː", "ю"),
    # дифтонги и долгие гласные
    ("eɪ", "эй"), ("aɪ", "ай"), ("ɔɪ", "ой"), ("aʊ", "ау"),
    ("əʊ", "оу"), ("oʊ", "оу"), ("ɪə", "иэ"), ("eə", "эа"), ("ʊə", "уэ"),
    ("iː", "и"), ("ɑː", "а"), ("ɔː", "о"), ("uː", "у"), ("ɜː", "ё"),
    # согласные из двух знаков
    ("tʃ", "ч"), ("dʒ", "дж"), ("ts", "ц"),
    # одиночные гласные
    ("ɪ", "и"), ("i", "и"), ("e", "э"), ("æ", "э"), ("ɒ", "о"), ("ɔ", "о"),
    ("ʊ", "у"), ("u", "у"), ("ʌ", "а"), ("ɐ", "а"), ("ə", "э"),
    ("a", "а"), ("o", "о"), ("ɛ", "э"), ("ɜ", "ё"),
    # согласные
    ("ʃ", "ш"), ("ʒ", "ж"), ("θ", "с"), ("ð", "з"), ("ŋ", "нг"),
    ("p", "п"), ("b", "б"), ("t", "т"), ("d", "д"), ("k", "к"),
    ("g", "г"), ("ɡ", "г"), ("f", "ф"), ("v", "в"), ("s", "с"),
    ("z", "з"), ("h", "х"), ("m", "м"), ("n", "н"), ("l", "л"),
    ("r", "р"), ("ɹ", "р"), ("w", "у"), ("j", "й"), ("x", "х"),
    ("ʔ", ""),
]

IOTATED = {"ю", "е", "я", "ё"}          # после согласной просят мягкий знак
IGNORE = set(".ˑ‿ʰʲ̃()-–—ˌ'")


def _tokens(ipa):
    """Режет транскрипцию на звуки: (знак IPA, кириллица, ударный, смещение).

    Смещение нужно, чтобы потом сопоставить звук с буквой в написании слова:
    без него непонятно, какой именно гласной досталась проглоченная «р».
    """
    out = []
    stressed = False
    i = 0
    while i < len(ipa):
        ch = ipa[i]
        if ch == "ˈ":
            stressed = True
            i += 1
            continue
        if ch.isspace():
            out.append((" ", " ", False, i))
            i += 1
            continue
        if ch in IGNORE:
            i += 1
            continue
        for src, dst in SOUNDS:
            if ipa.startswith(src, i):
                out.append((src, dst, stressed, i))
                if dst and dst[0] in VOWELS:
                    stressed = False
                i += len(src)
                break
        else:
            i += 1          # незнакомый знак пропускаем молча
    return out


def _silent_r(word, pronounced, need):
    """Где в написании стоят буквы r, которые британское произношение съело.

    Звучащие r берём первыми из тех, за которыми стоит гласная, — в rarely
    это первая r, а не вторая. Остальные и есть немые: их «р» возвращаем
    читателю, иначе rare превратится в «рэа», а water в «уотэ».
    """
    if need <= 0:
        return []
    word = word.lower()
    before_vowel = [m.start() for m in re.finditer("r", word)
                    if word[m.start() + 1:m.start() + 2] in tuple("aeiouy")]
    loud = set(before_vowel[:pronounced])
    quiet = [m.start() for m in re.finditer("r", word) if m.start() not in loud]
    return quiet[:need]


def _rhotic_positions(word, toks):
    """Какие гласные читаются с «р».

    Сопоставление идёт по месту в слове, а не по порядку: в water «р»
    у последней гласной (уотэр), а в artificial — у первой (артифишл).
    Раздача «по очереди» ошибалась то в одну, то в другую сторону.
    """
    pronounced = sum(1 for t in toks if t[0] in ("r", "ɹ"))
    need = word.lower().count("r") - pronounced
    letters = _silent_r(word, pronounced, need)
    if not letters:
        return {}

    span = max(t[3] for t in toks) + 1
    eligible = []
    for idx, t in enumerate(toks):
        if t[0] not in RHOTIC:
            continue
        # «э» тянет «р» только в последнем слоге: в salary за ней идёт
        # ещё одна гласная, и «р» там своя, звучащая
        if t[0] == "ə" and any(_sounds_vowel(x) for x in toks[idx + 1:]):
            continue
        eligible.append((idx, t[3] / span))

    out = {}
    for j in letters:
        if not eligible:
            break
        q = j / max(1, len(word))
        best = min(eligible, key=lambda e: abs(e[1] - q))
        out[best[0]] = True
        eligible.remove(best)
    return out


def from_ipa(ipa, word=""):
    """Переписывает транскрипцию кириллицей. Пустая строка, если разбирать
    нечего: звать нечитаемое «чтением» хуже, чем не показать ничего."""
    s = (ipa or "").strip().strip("/[]").strip()
    if not s:
        return ""
    word = (word or "").strip().lower()
    toks = _tokens(s)
    if not toks:
        return ""

    rhotic_at = _rhotic_positions(word, toks)

    out, stress_at, vowels = [], None, 0
    for idx, (src, dst, stressed, _pos) in enumerate(toks):
        if idx in rhotic_at:
            dst = RHOTIC[src]
        elif src == "ŋ":
            # перед k и g это обычное «н»: think → «синк», а не «сингк»
            nxt = toks[idx + 1][0] if idx + 1 < len(toks) else ""
            dst = "н" if nxt in ("k", "g", "ɡ") else "нг"
        elif src in ("n", "l") and _is_syllabic(toks, idx):
            # слоговые согласные: often → «офэн», able → «эйбэл»
            dst = "э" + dst
            vowels += 1
            if stressed and stress_at is None:
                stress_at = len(out)
        if (src.startswith("j") and dst and dst[0] in IOTATED
                and out and out[-1] and out[-1][-1] in CONSONANTS):
            dst = "ь" + dst          # queue → «кью», curious → «кьюриэс»
        if dst and dst[0] in VOWELS:
            vowels += 1
            if stressed:
                stress_at = len(out)
        out.append(dst)

    if stress_at is not None and vowels > 1:
        part = out[stress_at]
        pos = next((k for k, c in enumerate(part) if c in VOWELS), None)
        if pos is not None:
            out[stress_at] = part[:pos + 1] + STRESS + part[pos + 1:]

    return "".join(out).strip()


def _sounds_vowel(tok):
    """Звучит ли кусок гласной. Смотрим на результат, а не на знак IPA:
    «eɪ» и «juː» — гласные, хотя начинаются с согласных знаков."""
    return bool(tok[1]) and tok[1][0] in VOWELS


def _is_syllabic(toks, idx):
    """Согласная, образующая слог: в её слоге нет гласной.

    Так устроены often /ˈɒfn/ и little /ˈlɪtl/ — на письме гласная есть,
    в транскрипции её нет, и без неё чтение рассыпается: «офн», «литл».
    """
    if not idx:
        return False
    prev = toks[idx - 1]
    if _sounds_vowel(prev) or prev[0] in ("r", "ɹ", "j", "w"):
        return False
    return not any(_sounds_vowel(t) for t in toks[idx + 1:])


def show(row):
    """Чтение для карточки: сначала записанное руками, иначе пересчёт из IPA.

    Ручная запись важнее: у неё может быть своя причина — например, слово
    из урока записано так, как его произнёс преподаватель.
    """
    try:
        manual = (row["ru_read"] or "").strip()
    except (KeyError, IndexError):
        manual = ""
    if manual:
        return manual
    try:
        return from_ipa(row["ipa"], row["word"])
    except (KeyError, IndexError):
        return ""
