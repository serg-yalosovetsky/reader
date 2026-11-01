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
- схема http/https, без логина/пароля;
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

Исходный (не редиректный) запрос проверяется функцией `check_url` ДО отправки
(схема http/https, без credentials, все IP глобальные).
Response-хук `redirect_hooks` / `async_redirect_hooks` перехватывает все последующие редиректы,
чтобы запрос не ушёл во внутреннюю сеть через Location.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any

import anyio
import anyio.to_thread
import httpx

from .base import DownloaderError
from .remote_images import _allowed_ip

log = logging.getLogger("reader.redirect_guard")


class UrlBlocked(DownloaderError):
    """Адрес отклонён: внутренняя сеть или некорректная схема.

    Наследник DownloaderError, а не httpx.HTTPError: отказ не должен повторяться
    сетевыми ретраями.
    """

    def __init__(self, reason: str, url: str | httpx.URL | None = None) -> None:
        self.reason = reason
        self.url = str(url) if url is not None else ""
        msg = f"адрес {self.url} отклонён: {reason}" if self.url else f"адрес отклонён: {reason}"
        super().__init__(msg)


class UrlUnresolvable(UrlBlocked):
    """Хост не удалось разрешить через DNS."""

    def __init__(
        self,
        host: str,
        exc: Exception | str,
        url: str | httpx.URL | None = None,
    ) -> None:
        self.host = host
        self.orig_exc = exc
        super().__init__(f"не удалось разрешить {host}: {exc}", url=url)


class RedirectBlocked(UrlBlocked):
    """Редирект ведёт на недопустимый адрес; запрос по нему не отправлялся."""

    def __init__(self, message: str, reason: str = "") -> None:
        self.message = message
        self.reason = reason or message
        super().__init__(self.reason)
        self.args = (message,)


def host_matches(host: str, domain: str | tuple[str, ...] | list[str] | set[str]) -> bool:
    """Проверяет точное совпадение хоста или поддомен через точку.

    Предотвращает обходы вида evil-ficbook.net или evilsearchfloor.org.
    """
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    domains = (domain,) if isinstance(domain, str) else domain
    for d in domains:
        d_clean = (d or "").lower().rstrip(".")
        if d_clean and (h == d_clean or h.endswith("." + d_clean)):
            return True
    return False


def _reject(src: httpx.URL, dst: str, reason: str) -> RedirectBlocked:
    log.warning("редирект отклонён: %s -> %s: %s", src, dst, reason)
    return RedirectBlocked(f"редирект с {src.host} на {dst} отклонён: {reason}", reason=reason)


def check_url(url: str | httpx.URL) -> httpx.URL:
    """Проверить URL перед запросом: схема http/https, без credentials,
    все IP хоста глобальные (_allowed_ip). Возвращает httpx.URL или бросает UrlBlocked."""
    try:
        target = httpx.URL(url) if not isinstance(url, httpx.URL) else url
    except Exception as exc:
        log.warning("URL отклонён: %s: некорректный адрес (%s)", url, exc)
        raise UrlBlocked(f"некорректный адрес ({exc})", url=str(url)) from exc

    if target.scheme not in ("http", "https") or not target.host:
        log.warning("URL отклонён: %s: разрешены только http/https", target)
        raise UrlBlocked("разрешены только http/https", url=str(target))
    if target.userinfo:
        log.warning("URL отклонён: %s: в адресе не должно быть логина и пароля", target)
        raise UrlBlocked("в адресе не должно быть логина и пароля", url=str(target))
    port = target.port or (443 if target.scheme == "https" else 80)
    host = target.host.rstrip(".")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        # Fail closed: не смогли проверить — не идём (skip ≠ ok).
        log.warning("URL отклонён: %s: не удалось разрешить %s: %s", target, host, exc)
        raise UrlUnresolvable(host, exc, url=str(target)) from exc
    ips = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
    if not ips:
        log.warning("URL отклонён: %s: у %s нет IP-адресов", target, host)
        raise UrlBlocked(f"у {host} нет IP-адресов", url=str(target))
    bad = [str(ip) for ip in ips if not _allowed_ip(ip)]
    if bad:
        log.warning("URL отклонён: %s: внутренний адрес (%s)", target, ", ".join(bad))
        raise UrlBlocked(f"внутренний адрес ({', '.join(bad)})", url=str(target))
    return target


def check_redirect_target(src: httpx.URL, location: str) -> httpx.URL:
    """Проверить цель редиректа; вернуть абсолютный URL или бросить RedirectBlocked."""
    try:
        target = src.join(location)
    except httpx.InvalidURL as exc:  # кривой Location — отказ с причиной, не сбой
        raise _reject(src, location, f"некорректный адрес ({exc})") from exc
    try:
        return check_url(target)
    except UrlBlocked as exc:
        raise _reject(src, str(target), exc.reason) from exc


def _on_response(response: httpx.Response) -> None:
    if response.has_redirect_location:
        check_redirect_target(response.request.url, response.headers["Location"])


async def _async_on_response(response: httpx.Response) -> None:
    if response.has_redirect_location:
        # getaddrinfo блокирующий — уводим из event loop.
        await anyio.to_thread.run_sync(
            check_redirect_target, response.request.url, response.headers["Location"]
        )


def redirect_hooks() -> dict[str, list[Any]]:
    """event_hooks для httpx.Client: каждый хоп редиректа проходит проверку."""
    return {"response": [_on_response]}


def async_redirect_hooks() -> dict[str, list[Any]]:
    """event_hooks для httpx.AsyncClient: каждый хоп редиректа проходит проверку."""
    return {"response": [_async_on_response]}


def guarded_client(**kw: Any) -> httpx.Client:
    """httpx.Client с проверкой редиректов (свои event_hooks дополняются)."""
    hooks = dict(kw.pop("event_hooks", None) or {})
    hooks["response"] = [_on_response, *hooks.get("response", [])]
    return httpx.Client(event_hooks=hooks, **kw)


def guarded_async_client(**kw: Any) -> httpx.AsyncClient:
    """httpx.AsyncClient с асинхронной проверкой редиректов (свои event_hooks дополняются)."""
    hooks = dict(kw.pop("event_hooks", None) or {})
    hooks["response"] = [_async_on_response, *hooks.get("response", [])]
    return httpx.AsyncClient(event_hooks=hooks, **kw)


def requests_redirect_hook(response: Any, *args: Any, **kwargs: Any) -> None:
    """Хук response для requests/cloudscraper: проверяет редиректы и возвращает None.

    В requests возвращённое значение, отличное от None, заменяет объект ответа.
    Поэтому хук ОБЯЗАН возвращать None.
    """
    if getattr(response, "is_redirect", False) and "Location" in getattr(response, "headers", {}):
        check_redirect_target(httpx.URL(str(response.url)), response.headers["Location"])
    return None
