# -*- coding: utf-8 -*-
"""Всплывающая карточка слова. Frameless, поверх всех окон, не забирает фокус.

Базовый режим — показ: слово, транскрипция, перевод по таймеру. Поверх него
кнопками включаются режимы проверки (самопроверка, квиз, формы, ввод) —
они не заменяют показ, а запускаются по желанию прямо с карточки.
"""
import re

from PySide6.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve,
                            Signal, QPoint)
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QLabel, QPushButton, QLineEdit, QFrame,
                               QGraphicsDropShadowEffect, QApplication)

import aiworker
import db
import quiz
import ruread
import theme
import tts

# Базовые размеры рассчитаны на экран 1920x1080 при масштабе Windows 100%.
# На других экранах всё умножается на коэффициент из screen_scale().
CARD_W = 450

# Segoe UI — системный шрифт Windows, содержит полный набор символов IPA
# (ɔ, ɪ, ə и прочие), поэтому транскрипция рисуется одним шрифтом, без подмены.
# Без явного указания Qt берёт шрифт по умолчанию, который на части машин
# оказывается устаревшим MS Shell Dlg 2 с рваной кириллицей.
UI_FONT = theme.UI_FONT
# Палитра — из общего модуля: раньше цвета дублировались здесь и в шаблонах
# и разъезжались при правках.
C_BG         = theme.c("card")
C_WORD       = theme.c("word")
C_IPA        = theme.c("ipa")
C_READ       = theme.c("ruRead")
C_TRANS      = theme.c("translate")
C_EXAMPLE    = theme.c("example")
C_EXAMPLE_RU = theme.c("exampleRu")
C_META       = theme.c("muted")
C_FORM_TAG   = theme.c("formTag")
C_FORM_IPA   = theme.c("formIpa")
C_MODE       = theme.c("mode")


def screen_scale(screen):
    """Коэффициент размеров для конкретного экрана.

    Qt уже пересчитывает логические пиксели под масштабирование Windows,
    поэтому смотрим на логическую высоту: у ноутбука 768 карточка должна быть
    компактнее, у 4K без масштабирования (2160) — заметно крупнее.
    """
    manual = (db.get("card_scale", "auto") or "auto").strip()
    if manual != "auto":
        try:
            return max(0.7, min(2.0, float(manual)))
        except ValueError:
            pass
    if screen is None:
        return 1.0
    height = screen.availableGeometry().height()
    return max(0.82, min(1.7, round(height / 1080.0, 2)))


def build_style(k=1.0):
    """Собирает таблицу стилей под нужный масштаб."""
    def px(n):
        return max(8, int(round(n * k)))

    return f"""
    * {{ font-family: {UI_FONT}; }}
    #card       {{ background: {C_BG}; border: 1px solid #3a4150;
                  border-radius: {px(14)}px; }}
    #word       {{ color: {C_WORD}; font-size: {px(27)}px; font-weight: 600; }}
    #ipa        {{ color: {C_IPA}; font-size: {px(16)}px; }}
    #ruread     {{ color: {C_READ}; font-size: {px(17)}px;
                  margin: {px(2)}px 0 {px(2)}px 0; }}
    #trans      {{ color: {C_TRANS}; font-size: {px(19)}px;
                  margin: {px(6)}px 0 {px(9)}px 0; }}
    #hint       {{ color: #7b8496; font-size: {px(16)}px; font-style: italic;
                  margin: {px(6)}px 0 {px(9)}px 0; }}
    #example    {{ color: {C_EXAMPLE}; font-size: {px(15)}px; }}

    #exampleRu  {{ color: {C_EXAMPLE_RU}; font-size: {px(14)}px;
                  margin: {px(3)}px 0 0 0; }}
    #meta       {{ color: {C_META}; font-size: {px(11)}px; }}
    #formsBox   {{ background: #262b35; border: 1px solid #3a4150;
                  border-radius: {px(9)}px; }}
    #formTag    {{ color: {C_FORM_TAG}; font-size: {px(11)}px; font-weight: 600; }}
    #formTagV3  {{ color: #ffbe6b; font-size: {px(11)}px; font-weight: 600; }}
    #formWord   {{ color: #eef1f6; font-size: {px(17)}px; font-weight: 600; }}
    #formWordV3 {{ color: {C_TRANS}; font-size: {px(17)}px; font-weight: 600; }}
    #formIpa    {{ color: {C_FORM_IPA}; font-size: {px(12)}px; }}
    #formRead   {{ color: {C_READ}; font-size: {px(13)}px; }}
    #feedOk     {{ color: #3ddc91; font-size: {px(14)}px; font-weight: 600; }}
    #feedBad    {{ color: #ff6b6b; font-size: {px(14)}px; font-weight: 600; }}
    #modeTitle  {{ color: {C_IPA}; font-size: {px(11)}px; letter-spacing: 1px; }}
    QLineEdit   {{ background: #22262f; color: #ffffff; border: 1px solid #3a4150;
                  border-radius: {px(8)}px; padding: {px(8)}px {px(11)}px;
                  font-size: {px(16)}px; }}
    QLineEdit:focus {{ border-color: #3a86ff; }}
    QPushButton {{ background: #2a2f3a; color: #d6dbe6; border: 1px solid #3a4150;
                  border-radius: {px(8)}px; padding: {px(7)}px {px(14)}px;
                  font-size: {px(13)}px; }}
    QPushButton:hover  {{ background: #343b48; }}
    QPushButton#know   {{ background: #1f4d35; border-color: #2c6b49; color: #b9f2d1; }}
    QPushButton#know:hover  {{ background: #276242; }}
    QPushButton#again  {{ background: #4d2f1f; border-color: #6b452c; color: #f2d6b9; }}
    QPushButton#again:hover {{ background: #623b27; }}
    QPushButton#mode   {{ background: #23272f; border: 1px solid #3d4552;
                  color: {C_MODE}; padding: {px(5)}px {px(10)}px; font-size: {px(12)}px; }}
    QPushButton#mode:hover {{ color: #7fd4ff; border-color: #3a86ff; }}
    QPushButton#modeOn {{ background: #24344d; border: 1px solid #3a86ff; color: #9ecbff;
                  padding: {px(5)}px {px(10)}px; font-size: {px(12)}px; }}
    QPushButton#reveal {{ background: #24344d; border-color: #3a86ff; color: #9ecbff;
                  padding: {px(9)}px {px(14)}px; font-size: {px(14)}px; }}
    QPushButton#reveal:hover {{ background: #2c4060; }}
    QPushButton#opt    {{ background: #22262f; border: 1px solid #333a47; color: #d6dbe6;
                  padding: {px(9)}px {px(12)}px; font-size: {px(14)}px; text-align: left; }}
    QPushButton#opt:hover   {{ border-color: #3a86ff; background: #262c38; }}
    QPushButton#optOk  {{ background: #1f4d35; border: 1px solid #3ddc91; color: #d6ffe8;
                  padding: {px(9)}px {px(12)}px; font-size: {px(14)}px; text-align: left; }}
    QPushButton#optBad {{ background: #4d2029; border: 1px solid #ff6b6b; color: #ffd9d9;
                  padding: {px(9)}px {px(12)}px; font-size: {px(14)}px; text-align: left; }}
    #bar        {{ background: #3a86ff; border-radius: 2px; }}
    #barbg      {{ background: #262b35; border-radius: 2px; }}
    """


STYLE = build_style(1.0)

SHOW, SELFTEST, QUIZ, FORMS, TYPE = "show", "selftest", "quiz", "forms", "type"


class Popup(QWidget):
    answered = Signal(int, str, int)      # word_id, action, ms_visible
    unknown_word = Signal(str, str)       # незнакомое слово из примера, контекст

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._scale = 0.0          # 0 -> первый показ обязательно пересоберёт стиль
        self._style = STYLE
        self.setStyleSheet(self._style)

        self.row = None
        self.word_id = None
        self.mode = SHOW
        self._translation = ""
        self._sentence_id = None
        self._sentence_text = ""
        self._options = []
        self._answered = False
        self._elapsed = 0
        self._life_ms = 14000
        self._hover = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        self._outer = outer

        card = QFrame(objectName="card")
        shadow = QGraphicsDropShadowEffect(blurRadius=34, xOffset=0, yOffset=6)
        shadow.setColor(QColor(0, 0, 0, 190))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        v = QVBoxLayout(card)
        v.setContentsMargins(22, 16, 22, 14)
        v.setSpacing(7)
        self._inner = v

        top = QHBoxLayout()
        self.b_say = QPushButton("🔊", objectName="mode")
        self.b_say.setToolTip("Послушать слово и пример")
        self.b_say.setFixedWidth(34)
        self.b_say.clicked.connect(self._speak)
        self.l_word = QLabel(objectName="word")
        self.l_meta = QLabel(objectName="meta")
        self.l_meta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.l_word, 1)
        top.addWidget(self.b_say)
        top.addWidget(self.l_meta)
        v.addLayout(top)

        self.l_mode = QLabel(objectName="modeTitle")
        v.addWidget(self.l_mode)

        self.l_ipa = QLabel(objectName="ipa")
        v.addWidget(self.l_ipa)

        # Чтение русскими буквами стоит под транскрипцией и крупнее её:
        # пока IPA не выучена, читают именно эту строку.
        self.l_read = QLabel(objectName="ruread")
        v.addWidget(self.l_read)

        self.forms_box = self._build_forms()
        v.addWidget(self.forms_box)

        self.l_trans = QLabel(objectName="trans")
        self.l_trans.setWordWrap(True)
        v.addWidget(self.l_trans)

        self.l_ex = QLabel(objectName="example")
        self.l_ex.setWordWrap(True)
        # Каждое слово примера — ссылка: клик отправляет его на разбор
        # нейросети и оттуда в изучение.
        self.l_ex.setTextFormat(Qt.RichText)
        self.l_ex.setOpenExternalLinks(False)
        self.l_ex.linkActivated.connect(self._on_example_word)
        self.l_ex.setToolTip("Кликните на незнакомое слово — оно попадёт в изучение")
        v.addWidget(self.l_ex)

        self.l_ex_ru = QLabel(objectName="exampleRu")
        self.l_ex_ru.setWordWrap(True)
        v.addWidget(self.l_ex_ru)

        # --- зона взаимодействия: раскрытие, варианты, ввод ---
        self.b_reveal = QPushButton("Показать ответ", objectName="reveal")
        self.b_reveal.clicked.connect(self.reveal)
        v.addWidget(self.b_reveal)

        self.opts_box = QWidget()
        ov = QVBoxLayout(self.opts_box)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(6)
        self.opt_buttons = []
        for i in range(quiz.OPTIONS):
            b = QPushButton(objectName="opt")
            b.clicked.connect(lambda _, n=i: self.on_option(n))
            ov.addWidget(b)
            self.opt_buttons.append(b)
        v.addWidget(self.opts_box)

        self.input_box = QWidget()
        iv = QHBoxLayout(self.input_box)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(6)
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("напишите слово по-английски")
        self.inp.returnPressed.connect(self.on_typed)
        b_check = QPushButton("Проверить")
        b_check.clicked.connect(self.on_typed)
        iv.addWidget(self.inp, 1)
        iv.addWidget(b_check)
        v.addWidget(self.input_box)

        self.l_feed = QLabel(objectName="feedOk")
        self.l_feed.setWordWrap(True)
        v.addWidget(self.l_feed)

        # --- футер ---
        v.addSpacing(4)
        btns = QHBoxLayout()
        btns.setSpacing(7)
        self.b_know = QPushButton("✓  Знаю", objectName="know")
        self.b_again = QPushButton("↻  Ещё раз", objectName="again")
        self.b_know.setToolTip("Ctrl+Alt+1 — знаю, интервал растёт")
        self.b_again.setToolTip("Ctrl+Alt+2 — показать снова через 10 минут")
        self.b_know.clicked.connect(lambda: self.answer("know"))
        self.b_again.clicked.connect(lambda: self.answer("again"))
        btns.addWidget(self.b_know)
        btns.addWidget(self.b_again)
        btns.addStretch(1)
        self.b_next = QPushButton("→")
        self.b_next.setToolTip("Ctrl+Alt+3 — пропустить")
        self.b_next.clicked.connect(lambda: self.answer("skip"))
        btns.addWidget(self.b_next)
        v.addLayout(btns)

        modes = QHBoxLayout()
        modes.setSpacing(6)
        self.mode_buttons = {}
        for key, title, tip in (
                (SELFTEST, "Проверь меня", "Спрятать перевод и вспомнить самому"),
                (QUIZ, "Квиз", "Выбрать перевод из четырёх вариантов"),
                (FORMS, "Формы", "Вспомнить вторую и третью форму глагола"),
                (TYPE, "Угадай", "Написать слово по-английски"),
        ):
            b = QPushButton(title, objectName="mode")
            b.setToolTip(tip)
            b.clicked.connect(lambda _, k=key: self.set_mode(k))
            modes.addWidget(b)
            self.mode_buttons[key] = b
        modes.addStretch(1)
        v.addLayout(modes)

        self.barbg = QFrame(objectName="barbg")
        self.barbg.setFixedHeight(3)
        self.bar = QFrame(self.barbg, objectName="bar")
        self.bar.setFixedHeight(3)
        v.addWidget(self.barbg)

        self.setFixedWidth(CARD_W + 36)

        self._tick = QTimer(self)
        self._tick.setInterval(50)
        self._tick.timeout.connect(self._on_tick)

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal_translation)

        # Отложенный вердикт квиза и ввода. Обязательно управляемым таймером:
        # одноразовый singleShot пережил бы смену карточки и записал бы оценку
        # уже следующему слову.
        self._auto_action = None
        self._auto = QTimer(self)
        self._auto.setSingleShot(True)
        self._auto.timeout.connect(self._auto_answer)

        # Скрытие после угасания — тоже управляемым таймером: иначе отложенный
        # hide от прошлой карточки прячет только что показанную новую.
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

        self._anim = QPropertyAnimation(self, b"windowOpacity", self)

    def _speak(self):
        """Слово, а следом пример: слышно и произношение, и слово в живой фразе."""
        if not self.row:
            return
        text = self.row["word"]
        sentence = getattr(self, "_sentence_text", "")
        if sentence:
            text = f"{text}. {sentence}"
        tts.say(text)

    # ---------- масштаб под экран ----------
    def _current_screen(self):
        return QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()

    def apply_scale(self, screen):
        """Пересобирает стиль и геометрию, если карточка попала на другой экран."""
        k = screen_scale(screen)
        if abs(k - self._scale) < 0.01:
            return
        self._scale = k
        self._style = build_style(k)
        self.setStyleSheet(self._style)
        px = lambda n: max(1, int(round(n * k)))

        self._outer.setContentsMargins(px(18), px(18), px(18), px(18))
        self._inner.setContentsMargins(px(22), px(16), px(22), px(14))
        self._inner.setSpacing(px(7))
        self._forms_grid.setContentsMargins(px(12), px(8), px(12), px(8))
        self._forms_grid.setHorizontalSpacing(px(14))
        self.barbg.setFixedHeight(max(2, px(3)))
        self.bar.setFixedHeight(max(2, px(3)))

        # ширина карточки растёт с экраном, но не занимает больше трети ширины
        want = int(CARD_W * k) + px(36)
        limit = int(screen.availableGeometry().width() * 0.42) if screen else want
        self.setFixedWidth(max(px(330), min(want, limit)))

    # ---------- таблица трёх форм ----------
    def _build_forms(self):
        box = QFrame(objectName="formsBox")
        g = QGridLayout(box)
        self._forms_grid = g
        g.setContentsMargins(12, 8, 12, 8)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(1)
        self.form_tags, self.form_words = [], []
        self.form_ipas, self.form_reads = [], []
        for col, tag in enumerate(("V1", "V2", "V3")):
            # V3 выделена цветом: именно она нужна для Present Perfect
            t = QLabel(tag, objectName="formTagV3" if col == 2 else "formTag")
            w = QLabel(objectName="formWordV3" if col == 2 else "formWord")
            i = QLabel(objectName="formIpa")
            rd = QLabel(objectName="formRead")
            g.addWidget(t, 0, col)
            g.addWidget(w, 1, col)
            g.addWidget(i, 2, col)
            g.addWidget(rd, 3, col)
            g.setColumnStretch(col, 1)
            self.form_tags.append(t)
            self.form_words.append(w)
            self.form_ipas.append(i)
            self.form_reads.append(rd)
        return box

    def _fill_forms(self, hidden=False):
        r = self.row
        vals = [(r["word"], r["ipa"]), (r["v2"], r["ipa2"]), (r["v3"], r["ipa3"])]
        for col, (word, ipa) in enumerate(vals):
            hide = hidden and col > 0
            self.form_words[col].setText("?" if hide else (word or ""))
            self.form_ipas[col].setText("" if hide else (ipa or ""))
            self.form_reads[col].setText(
                "" if hide else ruread.from_ipa(ipa, word))

    # ---------- показ ----------
    def show_word(self, row):
        self.row = row
        self.word_id = row["id"]
        self._answered = False
        self._elapsed = 0
        self.mode = SHOW
        self._auto.stop()
        self._auto_action = None
        self._hide_timer.stop()

        self.l_word.setText(row["word"])
        self.b_say.setVisible(tts.is_enabled())
        tags = (row["tags"] or "").replace("tense:", "")
        self.l_meta.setText(" · ".join(x for x in [row["level"] or "", tags] if x))

        self._translation = row["translation"] or ""
        self._load_sentence(row)

        self.apply_mode()

        self._life_ms = max(4, db.get_int("popup_seconds", 14)) * 1000
        self.apply_scale(self._current_screen())
        self.adjustSize()
        self._place()

        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade(1.0, 220)

    # Список общий с очередью разбора: см. aiworker.STOP_WORDS — иначе
    # карточка и приёмник слов расходились бы в том, что считать служебным.
    STOP_WORDS = aiworker.STOP_WORDS


    def _load_sentence(self, row):
        """Берёт наименее показанный пример — слово каждый раз в новом предложении."""
        s = db.pick_sentence(row["id"])
        if s is None:
            self._sentence_id = None
            self.l_ex.setText(self._linkify(row["example_en"] or "", row["word"]))
            self.l_ex_ru.setText(row["example_ru"] or "")
            return
        self._sentence_id = s["id"]
        db.mark_sentence_shown(s["id"])
        self.l_ex.setText(self._linkify(s["text_en"], row["word"]))
        self.l_ex_ru.setText(s["text_ru"] or "")

    def _linkify(self, text, current_word):
        """Оборачивает слова примера в ссылки, кроме служебных и изучаемого."""
        if not text:
            return ""
        self._sentence_text = text
        cur = (current_word or "").lower()
        out = []
        for token in re.split(r"(\W+)", text):
            low = token.lower()
            if (token.isalpha() and len(token) > 2 and low not in self.STOP_WORDS
                    and not (cur and low.startswith(cur[:4]))):
                out.append(
                    f'<a href="w:{low}" style="color:{C_EXAMPLE};'
                    f'text-decoration:underline;">{token}</a>')
            else:
                out.append(token)
        return "".join(out)

    def _on_example_word(self, href):
        word = href.split(":", 1)[-1]
        if not word:
            return
        self.unknown_word.emit(word, getattr(self, "_sentence_text", ""))
        self.l_feed.setObjectName("feedOk")
        self.l_feed.setStyleSheet(self._style)
        self.l_feed.setText(f"«{word}» отправлено в изучение")
        self.adjustSize()
        self._place()

    @property
    def is_verb(self):
        return bool(self.row and self.row["v2"])

    def set_mode(self, mode):
        """Переключение режима с карточки. Повторное нажатие возвращает показ."""
        self.mode = SHOW if mode == self.mode else mode
        self._elapsed = 0
        self.apply_mode()
        self.adjustSize()
        self._place()

    def apply_mode(self):
        m = self.mode
        r = self.row
        self._reveal_timer.stop()
        self._tick.stop()
        self.l_feed.setText("")
        for key, b in self.mode_buttons.items():
            b.setObjectName("modeOn" if key == m else "mode")
            b.setStyleSheet(self._style)
            b.setVisible(key != FORMS or self.is_verb)

        titles = {SELFTEST: "САМОПРОВЕРКА — вспомните перевод",
                  QUIZ: "КВИЗ — выберите правильный перевод",
                  FORMS: "ФОРМЫ — вспомните вторую и третью",
                  TYPE: "УГАДАЙ — напишите слово по-английски"}
        self.l_mode.setText(titles.get(m, ""))
        self.l_mode.setVisible(m != SHOW)

        # что видно в каждом режиме
        self.l_word.setVisible(m != TYPE)
        self.l_ipa.setText(r["ipa"] or "")
        self.l_ipa.setVisible(bool(r["ipa"]) and not self.is_verb
                              and m in (SHOW, SELFTEST, FORMS))
        reading = ruread.show(r)
        self.l_read.setText(reading)
        self.l_read.setVisible(bool(reading) and not self.is_verb
                               and m in (SHOW, SELFTEST, FORMS))
        self.forms_box.setVisible(self.is_verb and m in (SHOW, FORMS))
        # Пример виден только в режиме показа: в проверке он содержит ответ
        # («He has drawn a portrait» выдаёт третью форму).
        self.l_ex.setVisible(bool(r["example_en"]) and m == SHOW)
        self.l_ex_ru.setVisible(bool(r["example_ru"]) and m == SHOW)
        self.b_reveal.setVisible(m in (SELFTEST, FORMS))
        self.opts_box.setVisible(m == QUIZ)
        self.input_box.setVisible(m == TYPE)
        self.barbg.setVisible(m == SHOW)
        self._show_answer_buttons(m == SHOW)

        if self.is_verb:
            self._fill_forms(hidden=(m == FORMS))

        if m == SHOW:
            self._start_show()
        elif m == SELFTEST:
            self.l_trans.setVisible(False)
            self.b_reveal.setText("Показать перевод")
        elif m == FORMS:
            self._reveal_translation()
            self.l_trans.setVisible(True)
            self.b_reveal.setText("Показать формы")
        elif m == QUIZ:
            self._start_quiz()
        elif m == TYPE:
            self._start_type()

    def _show_answer_buttons(self, visible):
        self.b_know.setVisible(visible)
        self.b_again.setVisible(visible)

    def _start_show(self):
        """Обычный режим: перевод сам появляется по таймеру — как и было."""
        self.l_trans.setVisible(True)
        delay = db.get_int("hide_translation_ms", 0)
        if delay > 0:
            self.l_trans.setObjectName("hint")
            self.l_trans.setText("… вспомни перевод")
            self.l_trans.setStyleSheet(self._style)
            self._reveal_timer.start(delay)
        else:
            self._reveal_translation()
        if not self._answered:
            self._tick.start()

    def _reveal_translation(self):
        self.l_trans.setObjectName("trans")
        self.l_trans.setText(self._translation)
        self.l_trans.setStyleSheet(self._style)

    def reveal(self):
        """Кнопка «Показать ответ» в самопроверке и режиме форм."""
        if self.mode == SELFTEST:
            self._reveal_translation()
            self.l_trans.setVisible(True)
            self.l_ex.setVisible(bool(self.row["example_en"]))
            self.l_ex_ru.setVisible(bool(self.row["example_ru"]))
        elif self.mode == FORMS:
            self._fill_forms(hidden=False)
            self.l_ex.setVisible(bool(self.row["example_en"]))
        self.b_reveal.setVisible(False)
        self._show_answer_buttons(True)
        self.adjustSize()
        self._place()

    # ---------- квиз ----------
    def _start_quiz(self):
        self.l_trans.setVisible(False)
        self._options = quiz.translation_options(self.row)
        if not self._options:
            self.l_feed.setObjectName("feedBad")
            self.l_feed.setStyleSheet(self._style)
            self.l_feed.setText("Слишком мало слов в базе для квиза")
            self.opts_box.setVisible(False)
            self._show_answer_buttons(True)
            return
        for b, text in zip(self.opt_buttons, self._options):
            b.setText(text)
            b.setObjectName("opt")
            b.setStyleSheet(self._style)
            b.setEnabled(True)

    def on_option(self, n):
        if n >= len(self._options):
            return
        correct = (self.row["translation"] or "").strip()
        chosen = self._options[n]
        for b in self.opt_buttons:
            b.setEnabled(False)
            if b.text() == correct:
                b.setObjectName("optOk")
            elif b.text() == chosen:
                b.setObjectName("optBad")
            b.setStyleSheet(self._style)
        self._finish_check(chosen == correct)

    # ---------- ввод ----------
    def _start_type(self):
        self._reveal_translation()
        self.l_trans.setVisible(True)
        self.inp.clear()
        self.inp.setEnabled(True)
        # единственный режим, где нужна клавиатура, — здесь фокус оправдан
        self.activateWindow()
        self.raise_()
        self.inp.setFocus()

    def on_typed(self):
        if not self.inp.isEnabled():
            return
        right = quiz.check_typed(self.inp.text(), self.row["word"])
        self.inp.setEnabled(False)
        self.l_word.setVisible(True)
        self._finish_check(right)

    def _finish_check(self, right):
        """Общий финал квиза и ввода: показать вердикт и засчитать ответ."""
        self.l_feed.setObjectName("feedOk" if right else "feedBad")
        self.l_feed.setStyleSheet(self._style)
        self.l_feed.setText("Верно!" if right
                            else f"Правильно: {self.row['word']} — {self._translation}")
        self.l_ex.setVisible(bool(self.row["example_en"]))
        self.adjustSize()
        self._place()
        self._auto_action = "know" if right else "again"
        self._auto.start(1900)

    def _auto_answer(self):
        if self._auto_action:
            action, self._auto_action = self._auto_action, None
            self.answer(action)

    # ---------- размещение и анимация ----------
    def _place(self):
        screen = self._current_screen()
        g = screen.availableGeometry()
        m = db.get_int("margin_px", 28)
        corner = db.get("corner", "top-center") or "top-center"
        w, h = self.width(), self.height()

        if "left" in corner:
            x = g.left() + m
        elif "right" in corner:
            x = g.right() - w - m
        else:
            x = g.left() + (g.width() - w) // 2

        if corner.startswith("top"):
            y = g.top() + m
        elif corner.startswith("bottom"):
            y = g.bottom() - h - m
        else:
            y = g.top() + (g.height() - h) // 2

        # на низком экране высокая карточка не должна уходить за границы
        x = max(g.left(), min(int(x), g.right() - w))
        y = max(g.top(), min(int(y), g.bottom() - h))
        self.move(QPoint(int(x), int(y)))

    def _fade(self, to, ms):
        self._anim.stop()
        self._anim.setDuration(ms)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(to)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._anim.start()

    # ---------- жизненный цикл ----------
    def _on_tick(self):
        if self._hover or self.mode != SHOW:   # в режимах проверки время не идёт
            return
        self._elapsed += self._tick.interval()
        frac = max(0.0, 1.0 - self._elapsed / self._life_ms)
        self.bar.setFixedWidth(int(self.barbg.width() * frac))
        if self._elapsed >= self._life_ms:
            self.answer("skip")

    def enterEvent(self, e):
        self._hover = True
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        # Клик по карточке отдаёт ей фокус — после него работают Enter/2/Esc.
        self.activateWindow()
        self.setFocus()
        super().mousePressEvent(e)

    def answer(self, action):
        if self._answered:
            return
        self._answered = True
        self._tick.stop()
        self._reveal_timer.stop()
        self._auto.stop()
        self._auto_action = None
        if self.word_id is not None:
            self.answered.emit(self.word_id, action, self._elapsed)
        self._fade(0.0, 180)
        self._hide_timer.start(200)

    def keyPressEvent(self, e):
        if self.mode == TYPE:
            if e.key() == Qt.Key_Escape:      # иначе из режима ввода не выйти
                self.answer("skip")
            else:
                super().keyPressEvent(e)
            return
        k = e.key()
        if k in (Qt.Key_Escape, Qt.Key_Right):
            self.answer("skip")
        elif k in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter) and self.b_reveal.isVisible():
            self.reveal()
        elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_1):
            self.answer("know")
        elif k in (Qt.Key_2, Qt.Key_Down):
            self.answer("again")
        else:
            super().keyPressEvent(e)
