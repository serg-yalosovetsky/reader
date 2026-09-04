"""Разово положить GMAIL_APP_PASSWORD из vault в /root/reader/.env.

Значение нигде не печатается: берётся из secrets-gateway по MESH_TOKEN и
дописывается в .env ридера (файл 600). Источник истины остаётся vault.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

ENV = "/root/reader/.env"
SECGW = "http://100.66.108.118:8783"
KEYS = {"READER_GMAIL_USER": None, "READER_GMAIL_APP_PASSWORD": "GMAIL_APP_PASSWORD"}
GMAIL_USER = "serg.yalosovetsky@gmail.com"


def mesh_token() -> str:
    out = subprocess.run(
        ["bash", "-lc", "set -a; . /root/.hermes/bitwarden-sm.env; set +a; printf %s \"$MESH_TOKEN\""],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def fetch(key: str, token: str) -> str:
    req = urllib.request.Request(f"{SECGW}/secrets/{key}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    for field in ("value", "secret", "data"):
        if isinstance(data.get(field), str):
            return data[field]
    raise SystemExit(f"secrets-gateway отдал неожиданный формат для {key}: {sorted(data)}")


def main() -> None:
    token = mesh_token()
    if not token:
        raise SystemExit("MESH_TOKEN не найден")
    body = open(ENV, encoding="utf-8").read()
    added = []
    for env_key, vault_key in KEYS.items():
        if re.search(rf"^{env_key}=", body, re.M):
            print(f"{env_key}: уже есть, не трогаю")
            continue
        value = GMAIL_USER if vault_key is None else fetch(vault_key, token)
        if not body.endswith("\n"):
            body += "\n"
        body += f"{env_key}={value}\n"
        added.append(env_key)
    if added:
        with open(ENV, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(ENV, 0o600)
    print("добавлено:", added or "ничего")
    names = re.findall(r"^([A-Z_]+)=", open(ENV, encoding="utf-8").read(), re.M)
    print("ключи .env теперь:", [n for n in names if "GMAIL" in n])


if __name__ == "__main__":
    sys.exit(main())
