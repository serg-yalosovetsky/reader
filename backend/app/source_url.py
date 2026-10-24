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

Редиректы: сам эндпоинт по адресу не ходит, поэтому проверить «куда уведёт
редирект» здесь нечем; защита — белый список хостов: подписка заводится только
на сайты-источники, а их загрузчики работают с этими же доменами.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from ..downloaders.fanficfare_engine import KNOWN_DOMAINS as _FFF_DOMAINS
from ..downloaders.remote_images import _allowed_ip

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
    return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)


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
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except OSError as exc:
        log.warning("зеркало: не удалось разрешить %s (%s): %s", host, url, exc)
        raise SourceUrlError(f"не удалось найти адрес сайта {host}: {exc}") from exc
    ips = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
    if not ips:
        log.warning("зеркало: у %s нет IP-адресов (%s)", host, url)
        raise SourceUrlError(f"у сайта {host} нет IP-адресов")
    bad = [str(ip) for ip in ips if not _allowed_ip(ip)]
    if bad:
        log.warning("зеркало: %s разрешается во внутренние адреса %s (%s)", host, bad, url)
        raise SourceUrlError(f"сайт {host} указывает на внутренний адрес ({', '.join(bad)})")
    return url
