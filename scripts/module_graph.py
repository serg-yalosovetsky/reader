"""Граф ES-модулей фронтенда: что откуда импортируется и на какой волне грузится.

Браузер узнаёт про модуль только разобрав того, кто его импортирует. Пять уровней
зависимостей = пять последовательных round-trip'ов до начала работы, независимо от
размера файлов. Скрипт показывает глубину (волну) каждого модуля и точный URL,
которым он импортируется — modulepreload обязан совпадать с ним посимвольно,
иначе браузер скачает файл дважды.

Запуск: cd /root/reader && python3 scripts/module_graph.py
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

ROOT = Path("/root/reader/frontend")
ENTRY = "/js/app.js"

# import ... from "x"; export ... from "x"; import("x")
STATIC = re.compile(
    r"""(?:^|[\s;}])(?:import|export)\b[^;'"]*?from\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
BARE = re.compile(r"""(?:^|[\s;}])import\s*['"]([^'"]+)['"]""", re.MULTILINE)
DYNAMIC = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")


def resolve(spec: str, importer: str) -> str | None:
    """URL-спецификатор → абсолютный путь-URL от корня фронтенда."""
    if spec.startswith(("http://", "https://", "data:")):
        return None
    if spec.startswith("/"):
        return spec
    base = Path(importer).parent
    return str((base / spec).resolve()) if str(base).startswith("/") else None


def local(url: str) -> Path:
    return ROOT / url.split("?")[0].lstrip("/")


def main() -> None:
    depth: dict[str, int] = {ENTRY: 0}
    kind: dict[str, str] = {ENTRY: "точка входа"}
    order: list[str] = [ENTRY]
    q = deque([ENTRY])

    while q:
        url = q.popleft()
        path = local(url)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found: list[tuple[str, str]] = []
        for m in STATIC.finditer(text):
            found.append((m.group(1), "статический"))
        for m in BARE.finditer(text):
            found.append((m.group(1), "статический"))
        for m in DYNAMIC.finditer(text):
            found.append((m.group(1), "динамический"))
        for spec, how in found:
            dep = resolve(spec, url)
            if not dep or dep in depth:
                continue
            depth[dep] = depth[url] + 1
            kind[dep] = how
            order.append(dep)
            q.append(dep)

    waves: dict[int, list[str]] = {}
    for url, d in depth.items():
        waves.setdefault(d, []).append(url)

    total = 0
    print(f"Модулей в графе: {len(depth)}, волн: {max(waves) + 1}\n")
    for d in sorted(waves):
        print(f"── волна {d} ──")
        for url in sorted(waves[d]):
            p = local(url)
            size = p.stat().st_size if p.exists() else -1
            total += max(size, 0)
            mark = "" if p.exists() else "  ⚠ ФАЙЛА НЕТ"
            dyn = "  [динамический]" if kind[url] == "динамический" else ""
            print(f"   {size:>7} б  {url}{dyn}{mark}")
    print(f"\nСуммарный вес: {total / 1024:.0f} КБ")
    print("\n--- готовые теги modulepreload (порядок = порядок волн) ---")
    for d in sorted(waves):
        for url in sorted(waves[d]):
            print(f'  <link rel="modulepreload" href="{url}" />')


if __name__ == "__main__":
    main()
