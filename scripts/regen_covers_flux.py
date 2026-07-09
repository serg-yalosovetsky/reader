#!/usr/bin/env python
"""Ночной батч: пересоздать обложки через локальный ComfyUI (FLUX.1-dev).

Ленивая генерация в читалке идёт на Pollinations (быстро, всегда доступно).
Этот скрипт запускается вручную, когда 3090 свободна, и заменяет
Pollinations-обложки на качественные FLUX. ComfyUI на SergPC виден с VPS через
tailscale serve (https://sergpc.tail939af1.ts.net:8188 → localhost:8188), URL
лежит в READER_COMFY_URL. Нужно лишь, чтобы ComfyUI был запущен. На VPS:

    cd /root/reader && set -a && . ./.env && set +a
    .venv/bin/python scripts/regen_covers_flux.py            # generated+gen_failed
    .venv/bin/python scripts/regen_covers_flux.py --only all # вообще все без реальной

Фолбэк без tailscale serve — обратный SSH-туннель с SergPC + --comfy-url:
    ssh -N -R 127.0.0.1:8188:127.0.0.1:8188 root@peaceful-albattani
    .venv/bin/python scripts/regen_covers_flux.py --comfy-url http://127.0.0.1:8188

Параметры: --only {generated,failed,missing,all}  --limit N  --comfy-url URL  --dry
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import config  # noqa: E402


def _select(session, only: str):
    from sqlmodel import select

    from backend.app.db.models import Work

    works = session.exec(select(Work).order_by(Work.id)).all()
    out = []
    for w in works:
        if not (w.title or "").strip():
            continue
        has_file = bool(w.cover_path) and Path(w.cover_path).exists()
        src = w.cover_source or ""
        if only == "all":
            # всё, кроме реальных обложек (embedded/source/description)
            if has_file and src not in ("generated", "gen_failed", ""):
                continue
            take = True
        elif only == "generated":
            take = src == "generated"
        elif only == "failed":
            take = src == "gen_failed"
        elif only == "missing":
            take = not has_file
        else:
            take = False
        if take:
            out.append(w)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--only", default="generated", choices=["generated", "failed", "missing", "all"]
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--comfy-url",
        default="",
        help="URL ComfyUI (по умолчанию READER_COMFY_URL из .env — "
        "tailscale serve https://sergpc...ts.net:8188)",
    )
    ap.add_argument("--dry", action="store_true", help="только показать список")
    args = ap.parse_args()

    # Батч всегда рисует через ComfyUI/FLUX, независимо от READER_IMAGE_PROVIDER.
    # По умолчанию берём READER_COMFY_URL из .env (tailscale serve); --comfy-url
    # перекрывает (напр. http://127.0.0.1:8188 при обратном туннеле).
    if args.comfy_url:
        config.COMFY_URL = args.comfy_url.rstrip("/")
    # Батч может ждать долго: холодный релоад модели после вытеснения VRAM.
    config.IMAGE_TIMEOUT = max(config.IMAGE_TIMEOUT, 900)

    from backend.app import covers, imagegen
    from backend.app.db.session import get_session

    if not imagegen._comfy_alive():
        print(
            f"[!] ComfyUI недоступен на {config.COMFY_URL} — "
            f"открой обратный туннель с SergPC и повтори.",
            file=sys.stderr,
        )
        return 2
    print(f"[i] ComfyUI жив: {config.COMFY_URL} (ckpt={config.COMFY_CKPT})")

    for session in get_session():
        targets = _select(session, args.only)
        if args.limit:
            targets = targets[: args.limit]
        n = len(targets)
        print(f"[i] к пересозданию ({args.only}): {n} книг")
        if args.dry:
            for w in targets:
                print(f"    #{w.id}  src={w.cover_source or '-'}  {w.title[:60]}")
            return 0

        from backend.app import imagegen

        ok = fail = 0
        t0 = time.monotonic()
        for i, w in enumerate(targets, 1):
            meta = {
                "title": w.title,
                "author": w.author,
                "genres": w.genres,
                "description": w.description,
                "fandom": w.fandom,
                "rating": w.rating,
                "cover_brief": w.cover_brief,
            }
            # Арт-бриф (Ollama) — раз на книгу, кешируем; идёт в промпт FLUX.
            if config.BRIEF_ENABLED and not (w.cover_brief or "").strip():
                brief = imagegen.summarize(meta)
                if brief:
                    w.cover_brief = brief
                    meta["cover_brief"] = brief
                    session.add(w)
                    session.commit()
            ts = time.monotonic()
            try:
                path = covers.generate_cover(meta, w.sha1, provider="comfy")
            except Exception as e:  # noqa: BLE001
                path = None
                print(f"  [{i}/{n}] #{w.id} ОШИБКА: {e}")
            if path:
                w.cover_path = str(path)
                w.cover_source = "generated"
                session.add(w)
                session.commit()
                ok += 1
                print(
                    f"  [{i}/{n}] #{w.id} OK {time.monotonic() - ts:.0f}s  {w.title[:50]}"
                )
            else:
                fail += 1
                print(f"  [{i}/{n}] #{w.id} пусто  {w.title[:50]}")
        dt = time.monotonic() - t0
        print(f"[i] готово: ok={ok} fail={fail} за {dt / 60:.1f} мин")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
