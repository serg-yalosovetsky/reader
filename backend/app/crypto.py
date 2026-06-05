"""Шифрование секретов аккаунтов (Fernet). Ключ — в файле вне репо (SECRET_KEY_PATH),
создаётся при первом обращении, права 600."""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

from .config import SECRET_KEY_PATH


def _key() -> bytes:
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes()
    SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    SECRET_KEY_PATH.write_bytes(key)
    try:
        os.chmod(SECRET_KEY_PATH, 0o600)
    except OSError:
        pass
    return key


def encrypt(plaintext: str) -> str:
    return Fernet(_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    return Fernet(_key()).decrypt(token.encode("ascii")).decode("utf-8")
