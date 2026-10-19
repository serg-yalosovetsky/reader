"""Встраивание внешних иллюстраций; одна сессия и бюджет на всю книгу."""
from __future__ import annotations

import html
import logging
import re
from io import BytesIO

from PIL import Image

logger = logging.getLogger(__name__)
MAX_IMAGE = 5 * 1024 * 1024
MAX_BOOK = 150 * 1024 * 1024
IMG = re.compile(r'''<img\b(?:[^"'<>]|"[^"]*"|'[^']*')*>''', re.IGNORECASE)
SRC = re.compile(r'''(\s+)([^\s=/>]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)''', re.IGNORECASE)


def _source(match):
    if match.group(2).lower() != 'src':
        return ''
    value = match.group(3)
    return html.unescape(value[1:-1] if value[:1] in ('"', "'") else value)


def remote_urls(markup: str) -> set[str]:
    return {_source(m) for tag in IMG.findall(markup) for m in SRC.finditer(tag)
            if re.match(r'https?://', _source(m), re.IGNORECASE)}


class ImageEmbedder:
    def __init__(self, fetch, work_id, prefix='images'):
        self.fetch = fetch
        self.work_id = work_id
        self.prefix = prefix
        self.cache = {}
        self.images = []
        self.total = 0

    def rewrite(self, markup: str, relative=lambda name: name) -> str:
        def source(match):
            url = _source(match)
            if not re.match(r'https?://', url, re.IGNORECASE):
                return match.group(0)
            if url not in self.cache:
                self.cache[url] = None
                try:
                    budget = min(MAX_IMAGE, MAX_BOOK - self.total)
                    if budget <= 0:
                        raise ValueError('исчерпан лимит картинок книги')
                    data = self.fetch(url, budget)
                    if len(data) > budget:
                        raise ValueError('картинка превышает лимит')
                    with Image.open(BytesIO(data)) as image:
                        fmt = image.format
                    formats = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'),
                               'GIF': ('gif', 'image/gif'), 'WEBP': ('webp', 'image/webp')}
                    ext, mime = formats[fmt]
                    name = f'{self.prefix}/i{len(self.images) + 1:04d}.{ext}'
                    self.images.append((name, data, mime))
                    self.total += len(data)
                    self.cache[url] = name
                except Exception as exc:  # noqa: BLE001 — Книга остаётся читаемой и при недоступной иллюстрации.
                    logger.warning('work_id=%s image=%s: %s', self.work_id, url, exc)
            name = self.cache[url]
            if name is None:
                return match.group(0)
            return f'{match.group(1)}{match.group(2)}="{html.escape(relative(name), quote=True)}"'
        return IMG.sub(lambda tag: SRC.sub(source, tag.group(0)), markup)


def fetch_image(client, url, limit):
    """Потоковое чтение ограничивает память даже при неверном Content-Length."""
    with client.stream('GET', url, timeout=15) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes(64 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError('картинка превышает лимит')
        return bytes(data)
