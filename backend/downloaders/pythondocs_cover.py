"""Обложка для книги из документации Python.

У документации обложки нет и быть не может: это не изданная книга. Пустая
карточка в библиотеке — плохо не эстетикой, а тем, что двенадцать разделов
становятся неразличимы, пока не прочитаешь подпись. Поэтому обложка рисуется
здесь: логотип Python (берётся из самого архива документации, `_static/og-image.png`),
название раздела и версия, плюс свой цвет-акцент на раздел — именно он и
отличает карточки друг от друга на расстоянии.

ИИ-генерация обложек в читалке есть (`covers.generate_cover`), но здесь она не
нужна и вредна: картинка «по мотивам» для справочника C API ничего не сообщает,
стоит запросов к модели и на каждой перекачке версии рисовалась бы заново.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

log = logging.getLogger("reader.pythondocs.cover")

WIDTH, HEIGHT = 600, 900
BG_TOP = (14, 22, 32)
BG_BOTTOM = (23, 36, 51)
TEXT = (240, 244, 248)
MUTED = (138, 154, 172)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Цвет-акцент на раздел: единственное, что различает карточки в сетке
# библиотеки на расстоянии, — не название мелким шрифтом, а полоса и подпись.
ACCENTS: dict[str, tuple[int, int, int]] = {
    "tutorial": (255, 212, 59),
    "library": (75, 139, 190),
    "reference": (167, 139, 250),
    "howto": (52, 211, 153),
    "using": (244, 114, 182),
    "installing": (251, 146, 60),
    "extending": (34, 211, 238),
    "c-api": (248, 113, 113),
    "faq": (163, 230, 53),
    "whatsnew": (96, 165, 250),
    "deprecations": (148, 163, 184),
    "misc": (192, 132, 252),
}
DEFAULT_ACCENT = (75, 139, 190)


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=font) <= max_width or not line:
            line = probe
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _tracked(draw, xy, text: str, font, fill, spacing: float) -> None:
    """Текст с разрядкой: у мелкой подписи капслоком она заменяет собой заголовок."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def render(title: str, version: str, part_key: str, logo_png: bytes | None = None) -> bytes | None:
    """PNG-обложка раздела. None — если рисовать нечем (нет Pillow или шрифтов).

    Отсутствие обложки не должно ронять сборку книги: без картинки книга
    читается, без книги картинка бесполезна.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover — Pillow объявлен в зависимостях
        log.warning("Pillow не установлен — обложка не будет нарисована")
        return None
    if not Path(FONT_BOLD).exists() or not Path(FONT_REGULAR).exists():
        log.warning("нет шрифтов DejaVu (%s) — обложка не будет нарисована", FONT_BOLD)
        return None

    accent = ACCENTS.get(part_key, DEFAULT_ACCENT)
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_TOP)
    draw = ImageDraw.Draw(img)

    # Фон — вертикальный градиент: ровная заливка на карточке выглядит как
    # незагрузившаяся картинка.
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)),
        )
    draw.rectangle([0, 0, 13, HEIGHT], fill=accent)

    # Логотип из самого архива документации (официальный, не срисованный).
    if logo_png:
        try:
            logo = Image.open(io.BytesIO(logo_png)).convert("RGBA")
            side = 200
            logo = logo.resize((side, side), Image.LANCZOS)
            img.paste(logo, ((WIDTH - side) // 2 + 6, 110), logo)
        except Exception as e:  # noqa: BLE001 — обложка не критична
            log.warning("логотип не вставился: %s", e)

    f_version = ImageFont.truetype(FONT_BOLD, 22)
    f_title = ImageFont.truetype(FONT_BOLD, 46)
    f_foot = ImageFont.truetype(FONT_REGULAR, 20)
    f_url = ImageFont.truetype(FONT_REGULAR, 18)

    label = f"PYTHON {version}"
    label_w = draw.textlength(label, font=f_version) + 4.0 * (len(label) - 1)
    _tracked(draw, ((WIDTH - label_w) / 2 + 6, 360), label, f_version, accent, 4.0)

    # Название раздела без префикса «Python — »: он повторяется в каждой книге и
    # съедает строку, которая нужна самому разделу.
    short = title.split("—", 1)[1].strip() if "—" in title else title
    lines = _wrap(draw, short, f_title, WIDTH - 110)
    y = 440
    for line in lines:
        w = draw.textlength(line, font=f_title)
        draw.text(((WIDTH - w) / 2 + 6, y), line, font=f_title, fill=TEXT)
        y += 58

    draw.line([(WIDTH / 2 - 60, y + 26), (WIDTH / 2 + 60, y + 26)], fill=accent, width=3)

    foot = "официальная документация"
    fw = draw.textlength(foot, font=f_foot)
    draw.text(((WIDTH - fw) / 2 + 6, HEIGHT - 120), foot, font=f_foot, fill=MUTED)
    url = "docs.python.org"
    uw = draw.textlength(url, font=f_url)
    draw.text(((WIDTH - uw) / 2 + 6, HEIGHT - 88), url, font=f_url, fill=(90, 105, 122))

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
