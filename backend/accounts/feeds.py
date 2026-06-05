"""Фиды обновлений подписок per-site (требуют залогиненной сессии).

Каждый адаптер: login(client, user, pw) -> bool; updates(client) -> [work_url, ...].
Найденные работы ставятся на отслеживание (Monitored); фактический детект новых
глав и докачку делает accounts/monitor.check_all (по числу глав, надёжно).
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup
from sqlmodel import Session

from . import monitor, store

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ----------------- ficbook -----------------
def _ficbook_login(c: httpx.Client, user: str, pw: str) -> bool:
    c.get("https://ficbook.net/")
    r = c.post("https://ficbook.net/login_check_static",
               data={"login": user, "password": pw})
    return "Войти используя аккаунт на сайте" not in r.text


def _ficbook_updates(c: httpx.Client) -> list[str]:
    r = c.post("https://ficbook.net/user_notifications/get_new",
               headers={"X-Requested-With": "XMLHttpRequest"})
    try:
        data = r.json()
    except ValueError:
        return []
    urls = []
    for n in (data.get("data", {}) or {}).get("notifications", []):
        url = n.get("url", "")
        # type 17 = обновления избранных авторов; берём всё, что ведёт на /readfic/.
        if "/readfic/" in url:
            urls.append("https://ficbook.net" + url)
    return urls


# ----------------- author.today -----------------
def _at_login(c: httpx.Client, user: str, pw: str) -> bool:
    page = c.get("https://author.today/account/login")
    token = _antiforgery(page.text)
    data = {"Login": user, "Password": pw, "RememberMe": "true"}
    if token:
        data["__RequestVerificationToken"] = token
    r = c.post("https://author.today/account/login", data=data,
               headers={"Referer": "https://author.today/account/login"})
    # после успеха обычно редирект/наличие меню профиля; грубая проверка:
    return "logOff" in r.text or "account/logoff" in r.text or r.url.path == "/feed"


def _antiforgery(html: str) -> str:
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


def _at_updates(c: httpx.Client) -> list[str]:
    r = c.get("https://author.today/feed")
    soup = BeautifulSoup(r.text, "lxml")
    urls = []
    for art in soup.select("article.feed-row"):
        header = art.select_one("h3.title") or art.select_one("header")
        htext = header.get_text(" ", strip=True) if header else ""
        # Берём обновления произведений и новые публикации; пропускаем подборки и пр.
        if "обновил произведение" in htext or "опубликовал новое произведение" in htext:
            a = art.select_one('a[href^="/work/"]')
            if a:
                m = re.match(r"/work/(\d+)", a.get("href", ""))
                if m:
                    urls.append(f"https://author.today/work/{m.group(1)}")
    return list(dict.fromkeys(urls))  # дедуп, сохраняя порядок


# ----------------- fanfics.me -----------------
def _fanfics_login(c: httpx.Client, user: str, pw: str) -> bool:
    c.get("https://fanfics.me/autent.php")
    r = c.post("https://fanfics.me/autent.php", data={"name": user, "pass": pw})
    return '<form name="autent"' not in r.text


def _fanfics_updates(c: httpx.Client) -> list[str]:
    # TODO: разведать страницу подписок/обновлений fanfics.me на реальной сессии.
    return []


_ADAPTERS = {
    "ficbook": (_ficbook_login, _ficbook_updates),
    "authortoday": (_at_login, _at_updates),
    "fanfics": (_fanfics_login, _fanfics_updates),
}


def fetch_site_updates(site: str, user: str, pw: str) -> list[str]:
    adapter = _ADAPTERS.get(site)
    if not adapter:
        return []
    login, updates = adapter
    with httpx.Client(timeout=40, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
        if not login(c, user, pw):
            raise RuntimeError(f"{site}: не удалось войти (проверьте логин/пароль)")
        return updates(c)


def pull_all(session: Session) -> dict:
    """Для каждого аккаунта забрать фид обновлений и поставить работы на отслеживание.
    Возвращает {site: {found, error?}}."""
    result = {}
    for site in _ADAPTERS:
        creds = store.creds_for_site(session, site)
        if not creds:
            continue
        try:
            urls = fetch_site_updates(site, *creds)
            for url in urls:
                monitor.add_monitor(session, url)
            store.touch_check(session, site)
            result[site] = {"found": len(urls)}
        except Exception as e:  # noqa: BLE001
            result[site] = {"error": str(e)[:200]}
    return result
