"""Фоновая обрезка PDF: один процесс poppler, ограниченные пачки, кэш по редакции."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-crop")
_LOCK = threading.RLock()
_JOBS: dict[str, concurrent.futures.Future] = {}
_RETRY: dict[str, float] = {}
FULL = [0, 0, 1, 1]


def content_box(image: Image.Image) -> list[float] | None:
    gray = image.convert("L")
    mask = gray.point(lambda x: 255 if x < 235 else 0)
    box = mask.getbbox()
    w, h = gray.size
    if not box or mask.histogram()[255] < w * h * .002:
        return None
    left, top, right, bottom = box
    if right - left < w * .1 or bottom - top < h * .1:
        return None
    pad = (right - left) * .025
    return [max(0, (left-pad)/w), max(0, (top-pad)/h),
            min(1, (right+pad)/w), min(1, (bottom+pad)/h)]


def stable_box(pages: list) -> list:
    boxes = [b for b in pages if b and (b[2]-b[0]) * (b[3]-b[1]) < .9]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else FULL


def cache_path(path: Path, sha1: str) -> Path:
    # Кэш — в каталоге данных читалки, а не рядом с книгой: книги лежат и в
    # библиотеке Calibre, посторонние файлы там ей не нужны. Хешируем
    # идентификатор: даже старое некорректное поле БД не становится путём.
    from .config import DATA_DIR

    key = hashlib.sha256(sha1.encode()).hexdigest()
    folder = DATA_DIR / "pdf-crop"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"v1-{key}.json"


def calculate(path: Path, sha1: str) -> dict:
    pages = []
    with tempfile.TemporaryDirectory(prefix="reader-crop-") as temp:
        root = Path(temp)
        # Читаем число страниц без загрузки PDF в память.
        info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True,
                              timeout=30, check=True)
        count = next(int(line.split(':', 1)[1]) for line in info.stdout.splitlines()
                     if line.startswith('Pages:'))
        for start in range(1, count + 1, 8):
            subprocess.run(["pdftoppm", "-r", "16", "-scale-to", "256", "-gray", "-f", str(start),
                            "-l", str(min(count, start+7)), str(path), str(root / "page")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=120, check=True)
            images = sorted(root.glob('page-*.pgm'), key=lambda p: int(p.stem.split('-')[-1]))
            if len(images) != min(8, count-start+1):
                raise ValueError("poppler не вернул все страницы")
            for image in images:
                with Image.open(image) as img:
                    pages.append(content_box(img))
                image.unlink()
    result = {"status": "ready", "book": stable_box(pages), "pages": pages}
    dest = cache_path(path, sha1)
    tmp = dest.with_suffix('.part')
    tmp.write_text(json.dumps(result), encoding='utf-8')
    tmp.replace(dest)
    return result


def ensure(path: Path, sha1: str) -> dict:
    dest = cache_path(path, sha1)
    try:
        return json.loads(dest.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    key = str(dest)
    with _LOCK:
        if key not in _JOBS and time.monotonic() >= _RETRY.get(key, 0):
            job = _POOL.submit(calculate, path, sha1)
            _JOBS[key] = job

            def finished(future):
                with _LOCK:
                    _JOBS.pop(key, None)
                    try:
                        future.result()
                    except Exception:
                        logging.getLogger(__name__).warning("Не удалось рассчитать поля PDF", exc_info=True)
                        _RETRY[key] = time.monotonic() + 300
                    else:
                        _RETRY.pop(key, None)

            job.add_done_callback(finished)
    return {"status": "pending", "book": None, "pages": []}
