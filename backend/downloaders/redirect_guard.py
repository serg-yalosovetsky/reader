"""Проверка каждого хопа редиректа у httpx-клиентов загрузчиков (serg/tasks#2330).

Хвост serg/tasks#1859. Белый список хостов (`app.source_url.validate_source_url`)
проверяет только исходный адрес, а загрузчики (author.today, readli, FicHub,
docs.python.org, searchfloor, webarticle, фиды подписок) ходят в httpx с
`follow_redirects=True`. Сайт-источник (или тот, кто его подменил) мог ответить
`302 Location: http://127.0.0.1:…/` — и reader послушно стучался во внутренние
сервисы VPS/tailnet.

Как защищаемся: в клиент вешается response-хук. httpx вызывает response-хуки
на КАЖДОМ ответе внутри цепочки редиректов и ДО того, как строит и отправляет
следующий запрос. Если ответ — редирект, хук проверяет цель:
- схема http/https, без логина/пароля, порт 80/443;
- все IP, в которые разрешается хост (или IP-литерал), глобальные — та же
  проверка, что у иллюстраций (`remote_images._allowed_ip`: никаких loopback,
  private, link-local, tailnet, IPv4 внутри IPv6-обёрток).
Не прошло — `RedirectBlocked` (DownloaderError) с причиной для человека и
warning в лог; запрос по редиректу не уходит.

Хост цели НЕ сверяется с белым списком источников: легальные редиректы уводят
на CDN и соседние домены (FicHub → хранилище EPUB, author.today → login), а
задача — не пустить запрос внутрь, а не ограничить интернет.

Остаточный риск: DNS rebinding между нашей проверкой и соединением httpx (имя
разрешается дважды). Пиннинга IP, как в `remote_images.fetch_image`, здесь нет —
он ломает прокси author.today (egress) и cookies сессий; закрывать отдельно,
если понадобится.

Исходный (не редиректный) запрос хук не проверяет: его адрес загрузчик строит
сам из фиксированного домена либо он уже прошёл `validate_source_url`.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any

import httpx

from .base import DownloaderError
from .remote_images import _allowed_ip

log = logging.getLogger("reader.redirect_guard")


class RedirectBlocked(DownloaderError):
    """Редирект ведёт на недопустимый адрес; запрос по нему не отправлялся.

    Наследник DownloaderError, а не httpx.HTTPError: сетевые ретраи загрузчиков
    (`_get` ловит httpx.HTTPError) не должны повторять заведомый отказ.
    """


def _reject(src: httpx.URL, dst: str, reason: str) -> RedirectBlocked:
    log.warning("редирект отклонён: %s -> %s: %s", src, dst, reason)
    return RedirectBlocked(f"редирект с {src.host} на {dst} отклонён: {reason}")


def check_redirect_target(src: httpx.URL, location: str) -> httpx.URL:
    """Проверить цель редиректа; вернуть абсолютный URL или бросить RedirectBlocked."""
    try:
        target = src.join(location)
    except httpx.InvalidURL as exc:  # кривой Location — отказ с причиной, не сбой
        raise _reject(src, location, f"некорректный адрес ({exc})") from exc
    if target.scheme not in ("http", "https") or not target.host:
        raise _reject(src, str(target), "разрешены только http/https")
    if target.userinfo:
        raise _reject(src, str(target), "в адресе не должно быть логина и пароля")
    port = target.port or (443 if target.scheme == "https" else 80)
    if port not in (80, 443):
        raise _reject(src, str(target), f"нестандартный порт {port}")
    host = target.host
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        # Fail closed: не смогли проверить — не идём (skip ≠ ok).
        raise _reject(src, str(target), f"не удалось разрешить {host}: {exc}") from exc
    ips = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
    if not ips:
        raise _reject(src, str(target), f"у {host} нет IP-адресов")
    bad = [str(ip) for ip in ips if not _allowed_ip(ip)]
    if bad:
        raise _reject(src, str(target), f"внутренний адрес ({', '.join(bad)})")
    return target


def _on_response(response: httpx.Response) -> None:
    if response.has_redirect_location:
        check_redirect_target(response.request.url, response.headers["Location"])


def redirect_hooks() -> dict[str, list[Any]]:
    """event_hooks для httpx.Client: каждый хоп редиректа проходит проверку."""
    return {"response": [_on_response]}


def guarded_client(**kw: Any) -> httpx.Client:
    """httpx.Client с проверкой редиректов (свои event_hooks дополняются)."""
    hooks = dict(kw.pop("event_hooks", None) or {})
    hooks["response"] = [_on_response, *hooks.get("response", [])]
    return httpx.Client(event_hooks=hooks, **kw)
