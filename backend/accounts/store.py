"""Хранение аккаунтов сайтов: пароль шифруется Fernet, наружу не отдаётся."""
from __future__ import annotations

from urllib.parse import urlparse

from sqlmodel import Session, select

from ..app.crypto import decrypt, encrypt
from ..app.db.models import Account, utcnow

# Хост → каноничный ключ сайта (для кредов и фидов). Отдельно от FanFicFare-карты,
# т.к. сюда входят и сайты со своими адаптерами (author.today/readli/searchfloor).
_SITE_BY_HOST = {
    "ficbook.net": "ficbook",
    "fanfics.me": "fanfics",
    "archiveofourown.org": "ao3",
    "fanfiction.net": "ffn",
    "author.today": "authortoday",
    "readli.net": "readli",
    "searchfloor.org": "searchfloor",
}


def site_of_host(host: str) -> str:
    """Хост (в т.ч. поддомен) → ключ сайта; иначе сам хост."""
    host = (host or "").lower()
    for dom, key in _SITE_BY_HOST.items():
        if host == dom or host.endswith("." + dom):
            return key
    return host


def site_of_url(url: str) -> str:
    return site_of_host(urlparse(url).hostname or "")


def upsert_account(session: Session, site: str, username: str, password: str) -> Account:
    """Создать/обновить аккаунт сайта (пароль шифруется)."""
    acc = session.exec(select(Account).where(Account.site == site)).first()
    if acc:
        acc.username = username
        acc.enc_secret = encrypt(password)
    else:
        acc = Account(site=site, username=username, enc_secret=encrypt(password))
        session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


def list_accounts(session: Session) -> list[dict]:
    """Аккаунты без секретов (для UI)."""
    accs = session.exec(select(Account)).all()
    return [
        {"id": a.id, "site": a.site, "username": a.username,
         "last_check": a.last_check}
        for a in accs
    ]


def delete_account(session: Session, account_id: int) -> bool:
    acc = session.get(Account, account_id)
    if not acc:
        return False
    session.delete(acc)
    session.commit()
    return True


def creds_for_site(session: Session, site: str) -> tuple[str, str] | None:
    acc = session.exec(select(Account).where(Account.site == site)).first()
    if not acc or not acc.enc_secret:
        return None
    try:
        return (acc.username, decrypt(acc.enc_secret))
    except Exception:  # noqa: BLE001 — повреждённый/несовместимый токен
        return None


def creds_for_host(session: Session, host: str) -> tuple[str, str] | None:
    return creds_for_site(session, site_of_host(host))


def set_cookies(session: Session, site: str, cookies: dict) -> None:
    """Сохранить cookie-сессию аккаунта (шифрованно)."""
    import json
    acc = session.exec(select(Account).where(Account.site == site)).first()
    if acc:
        acc.cookies = encrypt(json.dumps(cookies))
        session.add(acc)
        session.commit()


def get_cookies(session: Session, site: str) -> dict | None:
    import json
    acc = session.exec(select(Account).where(Account.site == site)).first()
    if not acc or not acc.cookies:
        return None
    try:
        return json.loads(decrypt(acc.cookies))
    except Exception:  # noqa: BLE001
        return None


def touch_check(session: Session, site: str) -> None:
    acc = session.exec(select(Account).where(Account.site == site)).first()
    if acc:
        acc.last_check = utcnow()
        session.add(acc)
        session.commit()
