# -*- coding: utf-8 -*-
"""Палитра: контраст текста и единый источник цветов.

На карточке текст сливался с фоном — подписи давали 2.5:1 при норме 4.5:1,
а кликабельные слова Qt красил в синий, дававший 1.9:1.
"""
import theme

TEXT_COLORS = ["text", "word", "dim", "muted", "example", "exampleRu",
               "ipa", "ruRead", "translate", "formTag", "formIpa", "mode"]


def test_every_text_color_is_readable():
    weak = {name: round(theme.contrast(theme.c(name)), 1)
            for name in TEXT_COLORS if theme.contrast(theme.c(name)) < 4.5}
    assert not weak, f"контраст ниже нормы: {weak}"


def test_css_variables_cover_the_palette():
    css = theme.css_vars()
    for name, value in theme.COLORS.items():
        assert f"--{name}: {value};" in css


def test_card_styles_use_the_shared_palette():
    import popup
    assert popup.C_EXAMPLE == theme.c("example")
    assert popup.C_META == theme.c("muted")
    assert popup.UI_FONT == theme.UI_FONT


def test_card_stylesheet_scales():
    import popup
    small, large = popup.build_style(1.0), popup.build_style(1.6)
    assert small != large
    assert "font-family" in small


def test_clickable_words_are_not_default_blue():
    """Таблица стилей Qt не действует на разметку внутри QLabel,
    поэтому цвет ссылки задаётся прямо в теге."""
    import io
    src = io.open("popup.py", encoding="utf-8").read()
    assert 'style="color:{C_EXAMPLE}' in src
    assert "#example a" not in src, "мёртвый селектор вводит в заблуждение"
