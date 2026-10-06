# -*- coding: utf-8 -*-
"""Единая палитра для карточки и веб-страниц.

Цвета жили в трёх местах — в стилях Qt, в дашборде и в тренировке — и молча
разъезжались: подняв контраст карточки, легко забыть про веб. Теперь источник
один, Qt берёт значения напрямую, а страницы получают их через /theme.css.

Контраст к фону карточки проверен по WCAG: у всего текста не ниже 4.5:1,
цифра рядом с цветом — измеренное значение.
"""

COLORS = {
    # фоны и границы
    "bg":         "#12151b",
    "panel":      "#191d25",
    "panel2":     "#1f242e",
    "card":       "#1c1f26",
    "line":       "#2b313d",
    "line2":      "#3a4150",

    # текст
    "text":       "#e6eaf2",   # 13.9:1
    "word":       "#ffffff",   # 16.5:1
    "dim":        "#9aa5b8",   #  6.6:1
    "muted":      "#8b94a6",   #  5.4:1

    # смысловые
    "accent":     "#3a86ff",   # 5.3:1 — для ссылок и рамок
    "accentFill": "#2f6fd0",   # заливка под белым текстом: 4.9:1
    "ipa":        "#8fdaff",   # 10.7:1
    "ruRead":     "#9fe0b8",   # 10.9:1 — чтение русскими буквами
    "translate":  "#ffd98a",   # 12.2:1
    "example":    "#d3d9e4",   # 11.6:1
    "exampleRu":  "#9aa5b8",   #  6.6:1
    "ok":         "#3ddc91",
    "warn":       "#ffb454",
    "bad":        "#ff6b6b",
    "formTag":    "#98a1b2",   #  6.3:1
    "formIpa":    "#84a9bd",   #  6.6:1
    "mode":       "#a3adbd",   #  7.3:1
}

# Системный шрифт Windows: содержит полный набор символов IPA (ɔ, ɪ, ə),
# поэтому транскрипция рисуется одним шрифтом, без подмены на лету.
UI_FONT = '"Segoe UI", "Segoe UI Variable Text", "Noto Sans", Arial, sans-serif'


def c(name):
    return COLORS[name]


def css_vars():
    """Палитра как CSS-переменные — подключается страницами через /theme.css."""
    lines = [f"  --{k}: {v};" for k, v in COLORS.items()]
    return ":root{\n" + "\n".join(lines) + "\n}\n"


def contrast(fg, bg=None):
    """Отношение контраста по WCAG — чтобы проверять палитру тестами."""
    bg = bg or COLORS["card"]

    def lum(h):
        h = h.lstrip("#")
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        f = lambda x: x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
        r, g, b = map(f, rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)
