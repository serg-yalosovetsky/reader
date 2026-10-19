"""Встраивание внешних иллюстраций; одна сессия и бюджет на всю книгу."""
from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
import time
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

logger = logging.getLogger(__name__)
MAX_IMAGE = 5 * 1024 * 1024
MAX_BOOK = 150 * 1024 * 1024
MAX_URLS = 300
IMAGE_DEADLINE = 30
TAILNET = ipaddress.ip_network("100.64.0.0/10")
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Устаревшие IPv4-compatible ::a.b.c.d — без исключений (mapped ::ffff:0:0/96 вне этой сети).
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
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
                if len(self.cache) >= MAX_URLS:
                    logger.warning('work_id=%s image=%s: исчерпан лимит %s URL книги',
                                   self.work_id, url, MAX_URLS)
                    return match.group(0)
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


def image_client(user_agent=None):
    """У иллюстраций нет доступа к сессии книги и прокси из окружения."""
    headers = {'User-Agent': user_agent} if user_agent else {}
    return httpx.Client(headers=headers, follow_redirects=False, trust_env=False)


def _embedded_ipv4(ip):
    """IPv4 внутри IPv6-обёрток (mapped, 6to4, Teredo, NAT64) проверяется отдельно."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _allowed_ip(ip):
    """Разрешено только глобальное, и внешний адрес, и вложенный IPv4 — каждый сам по себе."""
    # ::ffff:a.b.c.d — это и есть IPv4-адрес (сокет соединится с ним), судим только его.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _allowed_ip(ip.ipv4_mapped)
    if (not ip.is_global or ip.is_private or ip.is_reserved or ip.is_loopback
            or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip in TAILNET):
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local or ip in IPV4_COMPATIBLE:
            return False
        inner = _embedded_ipv4(ip)
        if inner is not None and not _allowed_ip(inner):
            return False
    return True


def validate_image_url(url):
    """Все адреса хоста должны быть глобальными; возвращает проверенный IP для соединения."""
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or port not in (80, 443)):
            raise ValueError('разрешены только http/https, порты 80/443, без учётных данных')
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise ValueError(f'недопустимый URL картинки {url}: {exc}') from exc
    if not addresses:
        raise ValueError(f'у хоста картинки нет IP-адресов: {parsed.hostname}')
    ips = [ipaddress.ip_address(address[0]) for _, _, _, _, address in addresses]
    for ip in ips:
        if not _allowed_ip(ip):
            raise ValueError(f'запрещённый IP картинки: {ip} ({parsed.hostname})')
    return ips[0]


def _pinned(url, ip):
    """URL с проверенным IP вместо имени: повторного разрешения (DNS rebinding) нет.
    Host и SNI остаются исходными — сертификат проверяется по имени хоста."""
    parsed = urlsplit(url)
    literal = f'[{ip}]' if ip.version == 6 else str(ip)
    netloc = literal + (f':{parsed.port}' if parsed.port else '')
    host = parsed.hostname + (f':{parsed.port}' if parsed.port else '')
    return parsed._replace(netloc=netloc).geturl(), host, parsed.hostname


def fetch_image(client, url, limit):
    """Общий дедлайн включает DNS, редиректы, заголовки и поток тела."""
    deadline = time.monotonic() + IMAGE_DEADLINE

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ValueError('превышен общий срок загрузки картинки (30 с)')
        return seconds

    for redirects in range(4):
        remaining()
        target, host, hostname = _pinned(url, validate_image_url(url))
        request = client.build_request('GET', target, headers={'Host': host},
                                       timeout=min(15, remaining()),
                                       extensions={'sni_hostname': hostname})
        # Не отправляем даже cookies, полученные от предыдущей иллюстрации.
        request.headers.pop('Cookie', None)
        response = client.send(request, stream=True, follow_redirects=False, auth=None)
        try:
            remaining()
            if response.status_code in (301, 302, 303, 307, 308):
                if redirects == 3:
                    raise ValueError('картинка: больше 3 редиректов')
                location = response.headers.get('Location')
                if not location:
                    raise ValueError('редирект картинки без Location')
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            data = bytearray()
            # Без накопления 64 КБ: проверяем время при каждом сетевом фрагменте.
            for chunk in response.iter_bytes():
                remaining()
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('картинка превышает лимит')
            remaining()
            return bytes(data)
        finally:
            response.close()
