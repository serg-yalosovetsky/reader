"""Фиды обновлений подписок per-site (требуют залогиненной сессии).

Каждый адаптер сам управляет сессией и логином, возвращает список URL работ.
Найденные работы ставятся на отслеживание (Monitored); детект новых глав и
докачку делает accounts/monitor.check_all.

ficbook закрыт анти-ботом (DDoS-Guard) — для него используем cloudscraper
(httpx/обычный requests получают страницу «Проверка безопасности»).
author.today с 2026-08 закрыт Cloudflare-челленджем для датацентровых IP, и
обходится он не клиентом, а egress'ом — см. downloaders/egress.at_client.
fanfics.me доступен обычным клиентом.
"""
from __future__ import annotations

import re
from contextlib import closing

import httpx
from bs4 import BeautifulSoup
from sqlmodel import Session

from ..downloaders import egress
from . import monitor, store

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _antiforgery(html: str) -> str:
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


def _cookies_dict(jar_holder) -> dict:
    """Куки → dict без конфликтов: дубли имён (домен/поддомен, напр. __ddg8_ на
    author.today и .author.today) валили _cookies_dict(c.cookies) — берём последнее."""
    jar = getattr(jar_holder, "jar", jar_holder)
    out = {}
    try:
        for ck in jar:
            out[ck.name] = ck.value
    except Exception:  # noqa: BLE001
        pass
    return out


def _ficbook_book_id(url: str) -> str | None:
    """Извлечь book_id из ficbook URL (работает и с chapter_id и без)."""
    # Id бывает числовым (12245524) и UUID (019cba23-6719-...). Старый \d+
    # обрезал UUID до ведущих цифр («019») → плодились подписки на битые URL.
    m = re.search(r'/readfic/([0-9a-f-]+)', url)
    return m.group(1) if m else None


# ----------------- ficbook (cloudscraper) -----------------
# (connect, read) — БЕЗ таймаута один транзиентный сталл ficbook/DDoS-Guard
# блокировал прогон навсегда внутри ThreadPoolExecutor APScheduler: future не
# завершался, счётчик max_instances заклинивало на 1 → все последующие тики
# скипались («maximum number of running instances reached»). Read = 90с щедро,
# т.к. под троттлингом ficbook легально отвечает десятки секунд.
_FICBOOK_TIMEOUT = (15, 90)


def _ficbook_feed(user: str, pw: str, cookies: dict | None = None) -> tuple[list[str], dict]:
    import cloudscraper
    # closing() обязателен: scraper — это requests.Session с keep-alive пулом.
    # Без close() каждый тик планировщика (15 мин) оставлял сокет в CLOSE-WAIT;
    # за ~10 дней упёрлись в RLIMIT_NOFILE=1024 и сайт перестал принимать
    # соединения (процесс жив, порт слушает, ответ — reset).
    with closing(cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows"})) as c:
        if cookies:
            c.cookies.update(cookies)

        def _notifications():
            rn = c.post("https://ficbook.net/user_notifications/get_new",
                        headers={"X-Requested-With": "XMLHttpRequest"}, timeout=_FICBOOK_TIMEOUT)
            try:
                return rn.json()
            except ValueError:
                return None

        # Переиспользуем сохранённую сессию; логинимся только если куки протухли.
        data = _notifications() if cookies else None
        if not data or "data" not in data:
            c.get("https://ficbook.net/", timeout=_FICBOOK_TIMEOUT)
            r = c.post("https://ficbook.net/login_check_static",
                       data={"login": user, "password": pw}, timeout=_FICBOOK_TIMEOUT)
            if "Войти используя аккаунт на сайте" in r.text or "Проверка безопасности" in r.text:
                raise RuntimeError("ficbook: не удалось войти")
            data = _notifications()

        urls = []
        for n in ((data or {}).get("data", {}) or {}).get("notifications", []):
            url = n.get("url", "")
            if "/readfic/" in url:
                book_id = _ficbook_book_id(url)
                if book_id:
                    urls.append(f"https://ficbook.net/readfic/{book_id}")
        return list(dict.fromkeys(urls)), _cookies_dict(c.cookies)


# ----------------- author.today -----------------
def _at_updates_from_feed(c) -> list[str]:
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


def _at_feed(user: str, pw: str, cookies: dict | None = None) -> tuple[list[str], dict]:
    # Если есть сохранённая cookie-сессия — используем её (без повторного входа,
    # который на новом устройстве требует email-код подтверждения).
    if cookies:
        with egress.at_client(timeout=40, follow_redirects=True, cookies=cookies,
                              headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
            feed = c.get("https://author.today/feed")
            if "account/logoff" in feed.text or "logOff" in feed.text:
                return _at_updates_from_feed(c), _cookies_dict(c.cookies)
        # cookie протухла — пробуем обычный вход ниже.
    with egress.at_client(timeout=40, follow_redirects=True,
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
        return _at_updates_from_feed(c), _cookies_dict(c.cookies)


# ----------------- fanfics.me -----------------
def _fanfics_feed(user: str, pw: str, cookies: dict | None = None) -> tuple[list[str], dict]:
    with httpx.Client(timeout=40, follow_redirects=True,
                      headers={"User-Agent": _UA}) as c:
        c.get("https://fanfics.me/autent.php")
        r = c.post("https://fanfics.me/autent.php", data={"name": user, "pass": pw})
        if '<form name="autent"' in r.text:
            raise RuntimeError("fanfics.me: не удалось войти")
        # TODO: разведать страницу обновлений подписок fanfics.me на живой сессии.
        return [], _cookies_dict(c.cookies)


_ADAPTERS = {
    "ficbook": _ficbook_feed,
    "authortoday": _at_feed,
    "fanfics": _fanfics_feed,
}


def fetch_site_updates(site: str, user: str, pw: str, cookies: dict | None = None) -> tuple[list[str], dict | None]:
    fn = _ADAPTERS.get(site)
    return fn(user, pw, cookies) if fn else ([], None)


def pull_all(session: Session, sites: list | None = None) -> dict:
    """Для каждого аккаунта забрать фид и поставить работы на отслеживание."""
    from sqlmodel import select
    from ..app.db.models import Monitored

    result = {}
    for site in (sites or list(_ADAPTERS.keys())):
        creds = store.creds_for_site(session, site)
        if not creds:
            continue
        cookies = store.get_cookies(session, site)
        try:
            urls, new_cookies = fetch_site_updates(site, creds[0], creds[1], cookies)
            # Для ficbook: пометить уже отслеживаемые книги как has_update=True
            # вместо поштучной проверки через FanFicFare (N запросов → 1 запрос).
            if site == "ficbook" and urls:
                notif_ids = {_ficbook_book_id(u) for u in urls} - {None}
                all_mons = session.exec(select(Monitored)).all()
                marked = 0
                for m in all_mons:
                    src = m.source_url or ""
                    if "ficbook.net" in src and _ficbook_book_id(src) in notif_ids:
                        if not m.has_update:
                            m.has_update = True
                            session.add(m)
                            marked += 1
                if marked:
                    session.commit()
            for url in urls:
                monitor.add_monitor(session, url)
            store.touch_check(session, site)
            if new_cookies:
                store.set_cookies(session, site, new_cookies)
            result[site] = {"found": len(urls)}
        except Exception as e:  # noqa: BLE001
            result[site] = {"error": str(e)[:200]}
    return result
