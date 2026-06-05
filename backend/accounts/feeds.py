"""Фиды обновлений подписок per-site (требуют залогиненной сессии).

Каждый адаптер сам управляет сессией и логином, возвращает список URL работ.
Найденные работы ставятся на отслеживание (Monitored); детект новых глав и
докачку делает accounts/monitor.check_all.

ficbook закрыт анти-ботом (DDoS-Guard) — для него используем cloudscraper
(httpx/обычный requests получают страницу «Проверка безопасности»).
author.today и fanfics.me доступны обычным клиентом.
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup
from sqlmodel import Session

from . import monitor, store

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _antiforgery(html: str) -> str:
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


# ----------------- ficbook (cloudscraper) -----------------
def _ficbook_feed(user: str, pw: str) -> list[str]:
    import cloudscraper
    c = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
    c.get("https://ficbook.net/")
    r = c.post("https://ficbook.net/login_check_static",
               data={"login": user, "password": pw})
    if "Войти используя аккаунт на сайте" in r.text or "Проверка безопасности" in r.text:
        raise RuntimeError("ficbook: не удалось войти")
    rn = c.post("https://ficbook.net/user_notifications/get_new",
                headers={"X-Requested-With": "XMLHttpRequest"})
    try:
        data = rn.json()
    except ValueError:
        return []
    urls = []
    for n in (data.get("data", {}) or {}).get("notifications", []):
        url = n.get("url", "")
        if "/readfic/" in url:
            urls.append("https://ficbook.net" + url)
    return list(dict.fromkeys(urls))


# ----------------- author.today -----------------
def _at_feed(user: str, pw: str) -> list[str]:
    with httpx.Client(timeout=40, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
        page = c.get("https://author.today/account/login")
        # токен именно из формы логина (на странице их несколько)
        m = re.search(r'id="loginForm".*?name="__RequestVerificationToken"[^>]*value="([^"]+)"',
                      page.text, re.S)
        token = m.group(1) if m else _antiforgery(page.text)
        data = {"__RequestVerificationToken": token, "Login": user, "Password": pw,
                "RememberMe": "true", "SendEmailIfNeeded": "false"}
        r = c.post("https://author.today/account/login", data=data,
                   headers={"Referer": "https://author.today/account/login",
                            "X-Requested-With": "XMLHttpRequest"})
        # Форма логина AJAX-овая, отдаёт JSON {isSuccessful, messages}.
        try:
            res = r.json()
        except ValueError:
            res = {}
        if not res.get("isSuccessful", False):
            msg = "; ".join(res.get("messages") or []) or "не удалось войти"
            raise RuntimeError(f"author.today: {msg}")
        feed = c.get("https://author.today/feed")
    soup = BeautifulSoup(feed.text, "lxml")
    urls = []
    for art in soup.select("article.feed-row"):
        header = art.select_one("h3.title") or art.select_one("header")
        htext = header.get_text(" ", strip=True) if header else ""
        if "обновил произведение" in htext or "опубликовал новое произведение" in htext:
            a = art.select_one('a[href^="/work/"]')
            if a and (m := re.match(r"/work/(\d+)", a.get("href", ""))):
                urls.append(f"https://author.today/work/{m.group(1)}")
    return list(dict.fromkeys(urls))


# ----------------- fanfics.me -----------------
def _fanfics_feed(user: str, pw: str) -> list[str]:
    with httpx.Client(timeout=40, follow_redirects=True,
                      headers={"User-Agent": _UA}) as c:
        c.get("https://fanfics.me/autent.php")
        r = c.post("https://fanfics.me/autent.php", data={"name": user, "pass": pw})
        if '<form name="autent"' in r.text:
            raise RuntimeError("fanfics.me: не удалось войти")
        # TODO: разведать страницу обновлений подписок fanfics.me на живой сессии.
        return []


_ADAPTERS = {
    "ficbook": _ficbook_feed,
    "authortoday": _at_feed,
    "fanfics": _fanfics_feed,
}


def fetch_site_updates(site: str, user: str, pw: str) -> list[str]:
    fn = _ADAPTERS.get(site)
    return fn(user, pw) if fn else []


def pull_all(session: Session) -> dict:
    """Для каждого аккаунта забрать фид и поставить работы на отслеживание."""
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
