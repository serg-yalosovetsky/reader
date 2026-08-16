"""Исходящий транспорт для сайтов, закрытых Cloudflare от датацентровых IP.

author.today встал за Cloudflare managed challenge (2026-08): ЛЮБОЙ запрос с
VPS — httpx, cloudscraper, сохранённые cookies — получает 403 с заголовком
`cf-mitigated: challenge` и страницей «Just a moment… Enable JavaScript and
cookies to continue». Тот же самый запрос с домашней ноды меша (OpenWrt,
резидентный IP) отдаёт 200.

Значит фильтр — репутация IP, а не отпечаток клиента, и headless-браузер или
FlareSolverr на VPS его не обходят: нужен другой egress. Поэтому запросы к AT
идут через SOCKS5-туннель VPS→домашняя нода (systemd `reader-egress-proxy`),
адрес — в READER_AT_PROXY.

Прокси НЕОБЯЗАТЕЛЕН: если переменная пуста или туннель лежит, клиент работает
напрямую — ровно так же, как до фикса. Сервис поднимается и на машине без
туннеля, просто AT снова упрётся в challenge.

Ficbook сюда НЕ заворачиваем: он проходит с VPS через cloudscraper (проверено),
а лишний хоп через домашний канал только замедлил бы скачивание.
"""

from __future__ import annotations

import os

import httpx

AT_PROXY_ENV = "READER_AT_PROXY"


def at_proxy() -> str | None:
    """Адрес SOCKS5/HTTP-прокси для author.today или None (идём напрямую)."""
    return (os.getenv(AT_PROXY_ENV) or "").strip() or None


def at_client(**kw) -> httpx.Client:
    """httpx.Client для author.today — через домашний egress, если он настроен.

    Вызывающий код передаёт свои timeout/headers/cookies как обычно; здесь
    добавляется только транспорт, чтобы не дублировать знание о прокси в
    девяти местах (downloader, фиды подписок, интерактивный вход).
    """
    proxy = at_proxy()
    if proxy:
        kw.setdefault("proxy", proxy)
    return httpx.Client(**kw)
