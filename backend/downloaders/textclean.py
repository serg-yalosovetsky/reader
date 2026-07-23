"""Чистка мусора источников-зеркал (readli и производные от них EPUB).

Три класса мусора, которые доезжали до читалки:

1. Служебные счётчики и реклама. readli на стыке страниц пагинации вставляет
   ``<!-- quoter = 1; -->`` плюс рекламные блоки (``<div caramel-id>``,
   ``div.dc-feed``, ``#yandex_rtb_*``). Комментарий переживал сборку EPUB и
   терял обёртку ``<!-- -->`` — читатель видел голое ``quoter = 0;``.
2. Слипшийся заголовок главы: источник отдаёт ``<h3>Глава перваяЭскадрон</h3>``
   без разделителя между номером и названием (и так же — в оглавлении).
3. Промо-трейлер author.today («P.S. Эта книга находится в процессе
   написания…») в конце последней главы.

Функции работают и на сыром HTML источника, и на уже собранном XHTML внутри
EPUB (для ретро-чистки библиотеки), поэтому убирают мусор в обоих видах —
и как разметку, и как вытекший текст.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment

# Вытекший счётчик цитирования readli — уже без обёртки комментария.
_QUOTER_TEXT_RE = re.compile(r"\bquoter\s*=\s*\d+\s*;?")

# Промо-трейлер AT в конце последней главы (вместе с предшествующим <hr/>).
# Ограничение по длине — страховка от съедания живого текста, если «Спасибо.»
# в этой книге отсутствует.
_PROMO_TAIL_RE = re.compile(
    r"(?:<hr\s*/?>)?\s*P\.\s*S\.\s*Эта книга находится в процессе написания"
    r".{0,600}?(?:Спасибо\.|$)",
    re.S | re.I,
)

# Разделитель между номером главы и её названием, если источник их склеил:
# «Глава перваяЭскадрон» → «Глава первая. Эскадрон». Срабатывает только на
# границе строчная/цифра → заглавная, поэтому нормальные заголовки
# («Глава третья. Открытый потенциал», «Глава десятая... а для всего…») не трогает.
_GLUED_TITLE_RE = re.compile(
    r"^(\s*(?:Глава|Часть|Том|Книга|Пролог|Эпилог|Интерлюдия)"
    r"(?:\s+(?:\d+|[А-Яа-яЁё]+(?:\s+[А-Яа-яЁё]+)?))?)(?=[А-ЯЁ])"
)

_AD_TAGS = ("script", "style", "ins", "iframe", "noscript")


def clean_title(title: str | None) -> str | None:
    """Починить слипшийся заголовок главы. None/пустое — как есть."""
    if not title:
        return title
    fixed = _GLUED_TITLE_RE.sub(r"\1. ", title, count=1)
    return re.sub(r"\s+", " ", fixed).strip()


def clean_html(html: str) -> str:
    """Убрать из фрагмента главы служебный/рекламный мусор источника."""
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")

    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for bad in soup.find_all(_AD_TAGS):
        bad.decompose()
    for div in soup.find_all("div"):
        classes = " ".join(div.get("class") or [])
        if (
            div.has_attr("caramel-id")
            or "dc-feed" in classes
            or (div.get("id") or "").startswith("yandex_rtb")
        ):
            div.decompose()

    out = str(soup)
    out = _PROMO_TAIL_RE.sub("", out)
    out = _QUOTER_TEXT_RE.sub("", out)
    # Пустые хвосты после вырезания: <br/> подряд и пробелы перед закрытием.
    out = re.sub(r"(?:\s*<br\s*/?>\s*){2,}$", "", out.rstrip())
    return out.strip()
