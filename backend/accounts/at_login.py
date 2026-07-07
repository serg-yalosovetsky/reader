"""Интерактивный вход в author.today с 2FA-кодом (для UI ридера).

/account/login у AT — двухстадийная AJAX-форма: первый POST (SendEmailIfNeeded=true)
шлёт код на почту, второй POST добавляет Code=<код> на ТОЙ ЖЕ сессии. Промежуточную
сессию (куки + antiforgery-токен + креды) держим в памяти между двумя вызовами API,
при успехе сохраняем куки через store.set_cookies (дальше их переиспользует _at_feed).

In-memory _pending корректен только при uvicorn --workers 1 (как reader.service).
"""
from __future__ import annotations

import re

import httpx
from sqlmodel import Session

from . import store

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_LOGIN = "https://author.today/account/login"
_pending: dict = {}


def _token(html: str) -> str:
    m = re.search(r'id="loginForm".*?name="__RequestVerificationToken"[^>]*value="([^"]+)"',
                  html, re.S)
    if m:
        return m.group(1)
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


def _post_login(c: httpx.Client, token: str, user: str, pw: str,
                code: str | None, send_email: bool) -> dict:
    data = {
        "__RequestVerificationToken": token,
        "Login": user, "Password": pw, "RememberMe": "true",
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


def start(user: str, pw: str) -> dict:
    """Стадия 1: логин с запросом кода. → code_sent | logged_in | error."""
    with httpx.Client(timeout=40, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
        page = c.get(_LOGIN)
        token = _token(page.text)
        res = _post_login(c, token, user, pw, code=None, send_email=True)
        cookies = dict(c.cookies)
    if res.get("isSuccessful"):
        _pending.pop("authortoday", None)
        return {"status": "logged_in", "_cookies": cookies, "_user": user, "_pw": pw}
    _pending["authortoday"] = {"cookies": cookies, "token": token, "user": user, "pw": pw}
    msg = "; ".join(res.get("messages") or []) or "Код отправлен на почту author.today"
    return {"status": "code_sent", "message": msg[:200]}


def submit_code(session: Session, code: str) -> dict:
    """Стадия 2: ввод кода на сохранённой сессии. При успехе сохраняет куки."""
    p = _pending.get("authortoday")
    if not p:
        return {"status": "error", "message": "Сессия входа истекла — начните заново"}
    with httpx.Client(timeout=40, follow_redirects=True, cookies=p["cookies"],
                      headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
        res = _post_login(c, p["token"], p["user"], p["pw"],
                          code=code.strip(), send_email=False)
        cookies = dict(c.cookies)
    if not res.get("isSuccessful"):
        msg = "; ".join(res.get("messages") or []) or "Неверный код"
        return {"status": "error", "message": msg[:200]}
    store.upsert_account(session, "authortoday", p["user"], p["pw"])
    store.set_cookies(session, "authortoday", cookies)
    _pending.pop("authortoday", None)
    return {"status": "logged_in"}
