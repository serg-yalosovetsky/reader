"""Автоматический вход в author.today: пароль из БД + код подтверждения из почты.

Зачем отдельный модуль. AT включает подтверждение входа по коду («вход с нового
устройства»), и парольный вход тогда возвращает isSuccessful=false. Прежний
downloaders.authortoday.download() логинился паролем на КАЖДУЮ книгу, результат
_login не проверял и молча работал анонимом — из-за чего любая книга 18+
выглядела как «нет доступа», хотя она бесплатна (serg/tasks#486).

Здесь сессия живёт долго: куки лежат в account.cookies (store), в памяти
процесса кэшируются на _TTL, а полный вход выполняется только когда куки
протухли. Код подтверждения читается из Gmail по IMAP (учётка в .env:
READER_GMAIL_USER / READER_GMAIL_APP_PASSWORD, источник истины — vault,
ключ GMAIL_APP_PASSWORD).

Два правила отбора письма, каждое закрывает свой способ подставить чужой код:
  * отправитель сверяется РАЗОБРАННЫМ адресом (parseaddr → домен author.today),
    а не подстрокой в заголовке: IMAP `FROM "author.today"` матчит и
    `"author.today" <mail@example.org>`;
  * берутся только письма с UID больше того, что был в ящике ДО отправки
    формы входа. По UID, а не по времени: часы почтового сервера и VPS
    расходятся, а UID в папке строго возрастают.

Живость сессии проверяется наличием userId на странице AT: у анонима его нет.
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import threading
import time
from email.header import decode_header
from email.utils import parseaddr

from sqlmodel import Session

from ..app.db.session import engine
from ..downloaders import egress
from . import store

log = logging.getLogger("reader.at_auto")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_BASE = "https://author.today"
_LOGIN = f"{_BASE}/account/login"
_SITE = "authortoday"

# Домен, письму с которого только и можно верить (сверяется разобранный адрес).
_MAIL_DOMAIN = "author.today"

# Сколько держим проверенную сессию без повторной проверки живости.
_TTL = float(os.environ.get("READER_AT_SESSION_TTL", "900"))
# Сколько ждём письмо с кодом (AT присылает за секунды, но почта бывает медленной).
_MAIL_WAIT = float(os.environ.get("READER_AT_MAIL_WAIT", "120"))
_MAIL_POLL = 5.0

_USERID_RE = re.compile(r"\buserId\s*:\s*(\d+)")
_TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
_CODE_RE = re.compile(r"код[^0-9]{0,40}?(\d{4,8})", re.I)

_lock = threading.Lock()
_cache: dict = {"cookies": None, "verified_at": 0.0}


# ---------------------------------------------------------------- почта

def _imap():
    """Соединение с Gmail с выбранным INBOX. None — учётки нет или почта недоступна."""
    user = os.environ.get("READER_GMAIL_USER", "")
    pw = os.environ.get("READER_GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        log.warning("at_auto: нет READER_GMAIL_USER/READER_GMAIL_APP_PASSWORD — код взять неоткуда")
        return None
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(user, pw)
        m.select("INBOX")
        return m
    except Exception as e:  # noqa: BLE001 — сеть/TLS/логин
        log.warning("at_auto: IMAP недоступен: %s", e)
        return None


def _close(m) -> None:
    try:
        m.logout()
    except Exception:  # noqa: BLE001
        pass


def _at_uids(m) -> list[int]:
    """UID писем, похожих на письма AT. Отбор грубый — точная сверка ниже, по From."""
    since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 3 * 86400))
    typ, data = m.uid("SEARCH", None, f'(FROM "{_MAIL_DOMAIN}" SINCE {since})')
    if typ != "OK":
        return []
    return [int(x) for x in (data[0] or b"").split()]


def mailbox_mark() -> int | None:
    """Наибольший UID письма AT прямо сейчас. 0 — писем нет, None — почта не ответила.

    Снимается ПЕРЕД отправкой формы входа: всё, что придёт после, — заведомо
    ответ на нашу попытку, а не старый код из ящика.

    Разница между 0 и None не косметическая: 0 значит «ящик пуст, любое новое
    письмо — наше», а None — «гарантии свежести нет вовсе». Если их смешать, то
    моргнувший на этом шаге IMAP молча превращает отбор по UID в «взять любой
    код из ящика», ровно ту дыру, ради которой отбор и вводился.
    """
    m = _imap()
    if m is None:
        return None
    try:
        uids = _at_uids(m)
        return max(uids) if uids else 0
    except Exception as e:  # noqa: BLE001
        log.warning("at_auto: не удалось снять отметку почты: %s", e)
        return None
    finally:
        _close(m)


def _mail_text(msg) -> str:
    """Всё текстовое содержимое письма одной строкой (plain + html без тегов)."""
    chunks = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ct == "text/html":
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = re.sub(r"<[^>]+>", " ", text)
        chunks.append(text)
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def _subject(msg) -> str:
    return "".join(
        (s.decode(enc or "utf-8", "replace") if isinstance(s, bytes) else s)
        for s, enc in decode_header(msg.get("Subject", ""))
    )


def _from_is_at(msg) -> bool:
    """Отправитель — действительно домен AT (разобранный адрес, не подстрока)."""
    addr = parseaddr(msg.get("From", ""))[1].lower()
    domain = addr.rpartition("@")[2]
    return domain == _MAIL_DOMAIN or domain.endswith("." + _MAIL_DOMAIN)


def _code_after_uid(after_uid: int) -> str | None:
    """Код подтверждения из письма AT новее after_uid. None — такого письма нет."""
    m = _imap()
    if m is None:
        return None
    try:
        for uid in sorted((u for u in _at_uids(m) if u > after_uid), reverse=True):
            typ, msg_data = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if not _from_is_at(msg):
                log.warning("at_auto: письмо uid=%s не от %s — пропускаю", uid, _MAIL_DOMAIN)
                continue
            if "подтверждение" not in _subject(msg).lower():
                continue  # «Вход в аккаунт …» — уведомление постфактум, кода в нём нет
            hit = _CODE_RE.search(_mail_text(msg))
            if hit:
                return hit.group(1)
        return None
    except Exception as e:  # noqa: BLE001 — IMAP капризен, автологин не должен ронять обход
        log.warning("at_auto: чтение почты не удалось: %s", e)
        return None
    finally:
        _close(m)


def _wait_code(after_uid: int) -> str | None:
    """Дождаться письма с кодом (AT шлёт его в момент первого POST)."""
    deadline = time.time() + _MAIL_WAIT
    while True:
        code = _code_after_uid(after_uid)
        if code:
            return code
        if time.time() >= deadline:
            return None
        time.sleep(_MAIL_POLL)


# ---------------------------------------------------------------- вход

def _post_login(c, token: str, user: str, pw: str, code: str | None, send_email: bool) -> dict:
    data = {
        "__RequestVerificationToken": token,
        "Login": user,
        "Password": pw,
        "RememberMe": "true",
        "SendEmailIfNeeded": "true" if send_email else "false",
    }
    if code:
        data["Code"] = code
    r = c.post(_LOGIN, data=data,
               headers={"Referer": _LOGIN, "X-Requested-With": "XMLHttpRequest"})
    try:
        return r.json()
    except ValueError:
        return {}


def _is_authed(cookies: dict | None) -> bool:
    """Живая ли сессия: у залогиненного на странице есть ненулевой userId."""
    if not cookies:
        return False
    try:
        with egress.at_client(timeout=30, follow_redirects=True, cookies=cookies,
                              headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
            r = c.get(_BASE)
            hit = _USERID_RE.search(r.text)
            return bool(hit and hit.group(1) != "0")
    except Exception as e:  # noqa: BLE001 — сеть
        log.warning("at_auto: проверка сессии не удалась: %s", e)
        return False


def _login_full() -> dict | None:
    """Полный вход: пароль, при запросе кода — код из почты. Куки в БД. None — не вышло."""
    with Session(engine) as s:
        creds = store.creds_for_host(s, "author.today")
    if not creds:
        log.warning("at_auto: учётки author.today нет в БД — вход невозможен")
        return None
    user, pw = creds
    mark = mailbox_mark()  # снимаем ДО формы: письмо с кодом придёт с UID больше
    with egress.at_client(timeout=40, follow_redirects=True,
                          headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
        page = c.get(_LOGIN)
        tm = _TOKEN_RE.search(page.text)
        if not tm:
            log.warning("at_auto: на странице входа нет antiforgery-токена")
            return None
        token = tm.group(1)
        res = _post_login(c, token, user, pw, code=None, send_email=True)
        if not res.get("isSuccessful"):
            msg = "; ".join(res.get("messages") or [])
            low = msg.lower()
            need_code = any(w in low for w in ("код", "code", "почт", "email", "e-mail", "подтвер"))
            if not need_code:
                log.warning("at_auto: вход отклонён: %s", msg[:200])
                return None
            if mark is None:
                # Отметку снять не удалось — свежесть кода ничем не подтверждена.
                # Лучше не войти, чем отправить протухший код из старого письма.
                log.warning("at_auto: почта не ответила при снятии отметки — "
                            "вход с кодом пропущен, повторим на следующем цикле")
                return None
            code = _wait_code(mark)
            if not code:
                log.warning("at_auto: код подтверждения не пришёл за %.0f с", _MAIL_WAIT)
                return None
            res = _post_login(c, token, user, pw, code=code, send_email=False)
            if not res.get("isSuccessful"):
                log.warning("at_auto: код не принят: %s",
                            "; ".join(res.get("messages") or [])[:200])
                return None
        cookies = dict(c.cookies)
    with Session(engine) as s:
        store.set_cookies(s, _SITE, cookies)
    log.info("at_auto: вход в author.today выполнен, куки сохранены")
    return cookies


def session_cookies(*, force: bool = False) -> dict | None:
    """Куки рабочей сессии AT. force=True — не верить кэшу и перелогиниться.

    Возвращает None, если войти не удалось: вызывающий тогда работает анонимом,
    как раньше (бесплатные книги качаются и без входа).
    """
    with _lock:
        now = time.time()
        if not force and _cache["cookies"] and now - _cache["verified_at"] < _TTL:
            return _cache["cookies"]

        if not force:
            with Session(engine) as s:
                saved = store.get_cookies(s, _SITE)
            if saved and _is_authed(saved):
                _cache.update(cookies=saved, verified_at=now)
                return saved

        fresh = _login_full()
        _cache.update(cookies=fresh, verified_at=time.time() if fresh else 0.0)
        return fresh
