"""Скачивание через FanFicFare (ficbook.net, fanfics.me, AO3, fanfiction.net и
сотни других сайтов). FanFicFare запускается в subprocess для изоляции памяти —
важно на VPS с дефицитом RAM: процесс отрабатывает и освобождает всё разом.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import time
import threading
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from ..app import progress
from .base import DownloaderError, DownloadResult, UnsupportedURL

# Отказы скачивания уходят в journald → Loki вместе с остальными строками
# сервиса: без них немой сбой невозможно разобрать даже задним числом
# (serg/tasks#986).
log = logging.getLogger("reader.download")

# Домены, которые FanFicFare покрывает и которые нам интересны в первую очередь.
# Список не исчерпывающий: FanFicFare поддерживает 100+ сайтов, но маршрутизацию
# делаем по известным нам, остальное уходит в FicHub-фоллбэк.
KNOWN_DOMAINS = {
    "ficbook.net": "ficbook",
    "fanfics.me": "fanfics",
    "archiveofourown.org": "ao3",
    "www.fanfiction.net": "ffn",
    "fanfiction.net": "ffn",
    "m.fanfiction.net": "ffn",
}


def _site_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return KNOWN_DOMAINS.get(host, host)


def supports(url: str) -> bool:
    """Быстрая проверка по домену (без запуска FanFicFare)."""
    host = (urlparse(url).hostname or "").lower()
    return host in KNOWN_DOMAINS


def _needs_cloudscraper(url: str) -> bool:
    """ficbook закрыт анти-ботом (DDoS-Guard) — нужен cloudscraper."""
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("ficbook.net")


def _ff_executable() -> list[str]:
    """Команда запуска FanFicFare. Через -c и cli.main, чтобы не зависеть от PATH
    и работать одинаково в venv на Windows и на Linux-VPS."""
    return [sys.executable, "-c", "from fanficfare.cli import main; main()"]


@contextlib.contextmanager
def _creds_config(creds: tuple[str, str] | None):
    """Yield `-c <inifile>` args carrying username/password in a 0600 temp INI, so the
    password never appears in argv / ps. `-o username=/password=` map to the [overrides]
    section, so this is behaviour-equivalent. Yields [] when there are no creds."""
    if not creds:
        yield []
        return
    fd, path = tempfile.mkstemp(prefix="fff-creds-", suffix=".ini")
    try:
        os.write(fd, f"[overrides]\nusername:{creds[0]}\npassword:{creds[1]}\n".encode())
        os.close(fd)
        os.chmod(path, 0o600)
        yield ["-c", path]
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def get_meta(
    url: str,
    *,
    creds: tuple[str, str] | None = None,
    timeout: int = 120,
    extra: dict | None = None,
) -> dict:
    """Метаданные без скачивания глав (FanFicFare --meta-only --json). Для детекта
    обновлений: возвращает dict с numChapters и пр. Пусто при ошибке.

    В ответе есть и `zchapters` — главы с датами публикации (по ним строятся
    даты в оглавлении, см. app/chapterdates.py). `extra` — опции `-o`, которыми
    вызывающий уточняет поведение (например формат даты главы).
    """
    cmd = _ff_executable() + [
        "-m", "--json-meta", "--non-interactive",
        "-o", "is_adult=true",
    ]
    for key, value in (extra or {}).items():
        cmd += ["-o", f"{key}={value}"]
    if _needs_cloudscraper(url):
        cmd += ["-o", "use_cloudscraper=true"]
    try:
        with _creds_config(creds) as cred_args:
            proc = subprocess.run(cmd + cred_args + [url],
                                  capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Молчание здесь стоило часов разбора: задание продолжало работу без
        # числа глав, и никто не знал, что метаданные вообще не получены.
        log.warning("метаданные %s не получены за %s с (таймаут)", url, timeout)
        return {}
    out = (proc.stdout or "").strip()
    if not out:
        log.warning(
            "метаданные %s пусты: код возврата %s; stderr: %s",
            url, proc.returncode, _strip_noise(proc.stderr)[-200:] or "(пусто)",
        )
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # На случай мусора до/после JSON — выдрать первый объект.
        start, end = out.find("{"), out.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(out[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def download(url: str, *, is_adult: bool = True, extra_options: dict | None = None) -> DownloadResult:
    """Скачать произведение в EPUB. Возвращает DownloadResult.

    Бросает UnsupportedURL, если FanFicFare не знает сайт (тогда цепочка пробует
    следующий загрузчик), или DownloaderError при иных сбоях.
    """
    workdir = Path(tempfile.mkdtemp(prefix="fff_"))
    cmd = _ff_executable() + [
        "-f", "epub",
        "--json-meta-file",          # метаданные рядом: <output>.json
        "--non-interactive",
        "-p",                        # «.» в stdout на каждый запрос — прогресс для панели
        "-o", f"is_adult={'true' if is_adult else 'false'}",
        "-o", "output_filename=book.${formatext}",
        "-o", "include_images=true",   # встраивать обложку (и иллюстрации) сайта
    ]
    if _needs_cloudscraper(url):
        cmd += ["-o", "use_cloudscraper=true"]
    creds = (extra_options or {}).pop("_creds", None) if extra_options else None
    for k, v in (extra_options or {}).items():
        cmd += ["-o", f"{k}={v}"]

    total = None
    if progress.active_job():
        # Только для фонового задания: число глав заранее — одним лёгким
        # проходом --meta-only. Не вышло — прогресс покажем в запросах.
        meta = get_meta(url, creds=creds, timeout=60)
        total = int(meta.get("numChapters") or 0) or None
        progress.report(
            0, total, "chapters" if total else "requests", "скачивание",
            title=meta.get("title") or None,
        )
    try:
        with _creds_config(creds) as cred_args:
            rc, out, err = _run_fff(cmd + cred_args + [url], workdir, 600, total=total)
    except subprocess.TimeoutExpired as e:
        raise DownloaderError(f"FanFicFare превысил тайм-аут на {url}") from e

    stderr = _strip_noise(err)
    # FanFicFare сообщает о незнакомом сайте характерным текстом.
    if "Failed to find adapter" in stderr or "No adapter found" in stderr:
        raise UnsupportedURL(stderr or f"FanFicFare не знает сайт: {url}")

    epubs = sorted(workdir.glob("*.epub"))
    if not epubs:
        # Нет файла — частые причины: требуется логин, защита Cloudflare, 0 глав,
        # фик удалён («Story does not exist» — FanFicFare пишет это в STDOUT).
        msg = _failure_reason(rc, out, err)
        raw = ((err or "") + "\n" + (out or "")).strip()
        log.warning(
            "FanFicFare не создал EPUB для %s: %s (код возврата %s; сырой вывод: %s)",
            url, msg[:400], rc, raw[-500:] or "пусто",
        )
        raise DownloaderError(f"Не удалось скачать {url}: {msg[:400]}")

    epub = epubs[0]
    meta = _read_meta(epub)
    return DownloadResult(
        file_path=epub,
        file_format="epub",
        title=meta.get("title", "") or epub.stem,
        author=meta.get("author", ""),
        site=_site_of(url),
        source_url=url,
        num_chapters=int(meta.get("numChapters", 0) or 0),
        extra={"workdir": str(workdir), "raw_meta": meta},
    )


def _run_fff(cmd: list[str], cwd: Path, timeout: int, *, total: int | None) -> tuple[int, str, str]:
    """Запустить FanFicFare, считая точки флага -p как прогресс (serg/tasks#893).

    -p печатает «.» в stdout на КАЖДЫЙ сетевой запрос — единственный прогресс,
    который FanFicFare отдаёт наружу. Точки в начале строки считаются и в текст
    не попадают: иначе они вклинились бы в причину отказа («.....Story does not
    exist»). stderr — во временный файл, не в непрочитанный PIPE: болтливый
    stderr переполнил бы пайп и повесил процесс. Таймаут сохраняется: по его
    истечении процесс убивается и поднимается TimeoutExpired.
    """
    state = {"dots": 0, "at_line_start": True}
    out_chars: list[str] = []
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=errf,
            text=True, encoding="utf-8", errors="replace",
        )

        def pump() -> None:
            for ch in iter(lambda: proc.stdout.read(1), ""):
                if ch == "." and state["at_line_start"]:
                    state["dots"] += 1
                    continue
                state["at_line_start"] = ch == "\n"
                out_chars.append(ch)

        reader = threading.Thread(target=pump, daemon=True, name="fff-stdout")
        reader.start()
        deadline = time.monotonic() + timeout
        while True:
            try:
                rc = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    reader.join(timeout=2)
                    raise subprocess.TimeoutExpired(cmd, timeout) from None
                _report_dots(state["dots"], total)
        reader.join(timeout=5)
        _report_dots(state["dots"], total)
        errf.seek(0)
        err = errf.read()
    return rc, "".join(out_chars), err


def _report_dots(dots: int, total: int | None) -> None:
    """Точки → прогресс. С известным числом глав: первые ~2 запроса — страница
    истории и метаданные, дальше запрос на главу."""
    if total:
        progress.report(min(max(dots - 2, 0), total), total, "chapters")
    else:
        progress.report(dots, None, "requests")


# Служебные строки, которые печатает не FanFicFare, а окружение интерпретатора:
# `zzz_sentry_bootstrap.pth` в venv сообщает об инициализации Sentry в stderr
# КАЖДОГО python-процесса. Раньше сообщение об ошибке строилось как
# `stderr or stdout`, и эта строка вытесняла настоящую причину («Story does not
# exist» в stdout) — и из лога, и из интерфейса (serg/tasks#888).
_NOISE_PREFIXES = ("[sentry_bootstrap]",)


def _strip_noise(text: str | None) -> str:
    lines = [
        ln for ln in (text or "").splitlines()
        if ln.strip() and not ln.lstrip().startswith(_NOISE_PREFIXES)
    ]
    return "\n".join(lines).strip()


def _reason(stdout: str | None, stderr: str | None) -> str:
    """Причина отказа из ОБОИХ потоков: FanFicFare пишет её то в stdout, то в stderr."""
    parts = [s for s in (_strip_noise(stderr), _strip_noise(stdout)) if s]
    return " | ".join(parts)


def _failure_reason(rc: int, stdout: str | None, stderr: str | None) -> str:
    """Причина отказа для человека и лога. Пустой она быть не может.

    FanFicFare обычно пишет причину сам («Story does not exist», требование
    логина). Но он умеет завершиться молча — с любым кодом возврата и пустыми
    потоками. Раньше в этом случае человек видел «EPUB не создан» и не мог даже
    понять, повторять ему или сломано навсегда (serg/tasks#986: сбой оказался
    разовым, повтор тем же кодом скачал книгу целиком).

    Порядок: настоящий текст → честное «промолчал, код такой-то». Сырой хвост
    потоков сюда НЕ идёт: он состоит из служебного шума venv, который однажды уже
    вытеснил настоящую причину из интерфейса (serg/tasks#888). Место сырого
    вывода — лог, туда он и пишется отдельной строкой.
    """
    text = _reason(stdout, stderr)
    if text:
        return text
    return f"EPUB не создан: FanFicFare промолчал (код возврата {rc})"


def _read_meta(epub: Path) -> dict:
    """Прочитать соседний <epub>.json с метаданными FanFicFare."""
    meta_file = epub.with_name(epub.name + ".json")
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}
