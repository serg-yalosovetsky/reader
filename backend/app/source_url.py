"""Проверка адреса книги, который человек вводит руками (serg/tasks#1859).

Эндпоинт зеркала (`POST /api/library/{id}/mirror`) раньше принимал любой
http/https-адрес и заводил на него подписку: монитор дальше сам ходил по этому
адресу с сервера. Это SSRF — ссылкой вида http://127.0.0.1:…/ или
http://169.254.169.254/ можно было заставить reader стучаться во внутренние
сервисы VPS и tailnet.

Правила:
- только http/https, без логина/пароля в адресе, порт только 80/443;
- хост — из списка сайтов, которые reader реально умеет читать и мониторить
  (см. `SUPPORTED_DOMAINS`), точное совпадение или поддомен;
- все IP, в которые разрешается хост, — глобальные (никаких loopback, private,
  link-local, tailnet, IPv4 внутри IPv6-обёрток) — та же проверка, что у
  картинок (`downloaders.remote_images._allowed_ip`).

Редиректы: сам эндпоинт по адресу не ходит; каждый хоп редиректа проверяют
httpx-клиенты загрузчиков (`downloaders.redirect_guard`, serg/tasks#2330).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from ..downloaders.fanficfare_engine import KNOWN_DOMAINS as _FFF_DOMAINS
from ..downloaders.redirect_guard import (
    UrlBlocked,
    UrlUnresolvable,
    check_url,
    host_matches,
)

log = logging.getLogger("reader.source_url")

# Сайты, у которых есть и загрузчик, и счётчик глав для монитора
# (downloaders/chain.py → fetch, accounts/monitor.py → _chapter_count):
# - свои адаптеры: author.today, readli.net, searchfloor.org, docs.python.org;
# - FanFicFare (fanficfare_engine.KNOWN_DOMAINS): ficbook.net, fanfics.me,
#   archiveofourown.org, fanfiction.net.
# Остальное FanFicFare/FicHub/webarticle берут «на удачу» при разовой загрузке,
# но мониторить такие адреса монитор не умеет — зеркало на них бессмысленно.
_OWN_ADAPTERS = ("author.today", "readli.net", "searchfloor.org", "docs.python.org")
SUPPORTED_DOMAINS: tuple[str, ...] = tuple(
    sorted({*_OWN_ADAPTERS, *(d.removeprefix("www.").removeprefix("m.") for d in _FFF_DOMAINS)})
)


class SourceUrlError(ValueError):
    """Адрес отвергнут; текст исключения — причина для человека."""


def _supported_host(host: str) -> bool:
    return host_matches(host, SUPPORTED_DOMAINS)


def validate_source_url(url: str) -> str:
    """Вернуть нормализованный (обрезанный) адрес или бросить SourceUrlError."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:  # кривой порт/IPv6-литерал — это ответ человеку, не сбой
        raise SourceUrlError(f"не похоже на ссылку: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceUrlError("нужна ссылка на книгу (http/https)")
    if parts.username is not None or parts.password is not None:
        raise SourceUrlError("в ссылке не должно быть логина и пароля")
    if port is not None and port not in (80, 443):
        raise SourceUrlError(f"нестандартный порт {port} не поддерживается")
    host = parts.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # не IP-литерал — обычное имя, проверяем ниже по белому списку
    else:
        raise SourceUrlError("нужна ссылка на сайт-источник, а не на IP-адрес")
    if not _supported_host(host):
        raise SourceUrlError(
            f"сайт {host} не поддерживается для зеркала; подходят: "
            + ", ".join(SUPPORTED_DOMAINS)
        )
    try:
        check_url(url)
    except UrlUnresolvable as exc:
        log.warning("зеркало: отклонён адрес %s: %s", url, exc.reason)
        raise SourceUrlError(f"не удалось найти адрес сайта {host}: {exc.reason}") from exc
    except UrlBlocked as exc:
        log.warning("зеркало: отклонён адрес %s: %s", url, exc.reason)
        raise SourceUrlError(f"сайт {host} указывает на {exc.reason}") from exc
    return url
