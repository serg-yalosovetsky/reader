"""Скачивание через FanFicFare (ficbook.net, fanfics.me, AO3, fanfiction.net и
сотни других сайтов). FanFicFare запускается в subprocess для изоляции памяти —
важно на VPS с дефицитом RAM: процесс отрабатывает и освобождает всё разом.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
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


# Инструментовка сервера, которая через окружение едет в КАЖДЫЙ дочерний
# python-процесс (serg/tasks#1525). Сервис стартует под `opentelemetry-instrument`:
# тот кладёт каталог `auto_instrumentation` (с sitecustomize) в PYTHONPATH, а в
# venv лежит `zzz_sentry_bootstrap.pth`, который поднимает sentry_sdk при
# непустом SENTRY_DSN. Дочерний FanFicFare наследовал всё это и на старте
# каждого запуска импортировал OTel-дистро со всеми инструментаторами и Sentry:
# одна ficbook-ссылка get_meta стоила 3.94 с CPU против 1.39 с в чистом env,
# тик монитора — 240 с CPU. Трейсы дочерних процессов никто не смотрит, а ошибки
# FanFicFare и так доходят до сервера (код возврата, stderr) и логируются им.
#
# Список ЗАПРЕЩАЮЩИЙ, а не разрешающий: прокси (HTTP(S)_PROXY), READER_*, HOME,
# локаль и прочее окружение дочерний процесс получает как раньше. Именно
# очистка, а не OTEL_SDK_DISABLED: флаг выключает SDK, но импорт дистро и
# инструментаторов (основная стоимость) всё равно происходит.
_CHILD_ENV_DROP = frozenset({"SENTRY_DSN"})
_CHILD_ENV_DROP_PREFIXES = ("OTEL_",)
_CHILD_PYTHONPATH_DROP = "auto_instrumentation"


def _child_env() -> dict[str, str]:
    """Окружение дочернего процесса FanFicFare без инструментовки сервера."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in _CHILD_ENV_DROP and not k.startswith(_CHILD_ENV_DROP_PREFIXES)
    }
    pythonpath = env.get("PYTHONPATH")
    if pythonpath is not None:
        kept = [
            part for part in pythonpath.split(os.pathsep)
            if _CHILD_PYTHONPATH_DROP not in part
        ]
        if any(kept):
            env["PYTHONPATH"] = os.pathsep.join(kept)
        else:
            del env["PYTHONPATH"]
    return env


def _meta_args(url: str, extra: dict | None) -> list[str]:
    """Аргументы FanFicFare для `--json-meta` (без исполняемого файла, кредов и URL).

    Общие для отдельного процесса (`get_meta`) и драйвера (`MetaDriver`): оба
    пути обязаны спрашивать FanFicFare ОДНО И ТО ЖЕ.
    """
    args = [
        "-m", "--json-meta", "--non-interactive",
        "-o", "is_adult=true",
    ]
    for key, value in (extra or {}).items():
        args += ["-o", f"{key}={value}"]
    if _needs_cloudscraper(url):
        args += ["-o", "use_cloudscraper=true"]
    return args


def _parse_meta(url: str, rc: int | None, stdout: str | None, stderr: str | None) -> dict:
    """Ответ FanFicFare `--json-meta` → dict. Пусто при ошибке, и каждая ошибка в логе."""
    out = (stdout or "").strip()
    if not out:
        log.warning(
            "метаданные %s пусты: код возврата %s; stderr: %s",
            url, rc, _strip_noise(stderr)[-200:] or "(пусто)",
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
                pass
    log.warning("метаданные %s не разобраны: код возврата %s; stdout: %s", url, rc, out[:200])
    return {}


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

    Отдельный процесс на вызов. Для серии ссылок (фаза счёта глав монитора) —
    `MetaDriver`: тот же ответ без старта интерпретатора на каждую ссылку.
    """
    cmd = _ff_executable() + _meta_args(url, extra)
    try:
        # `-m` всё равно пишет ПУСТОЙ EPUB в рабочий каталог процесса: запуск из
        # cwd сервиса копил `*-fbn_*.epub` прямо в /opt/reader (77 штук на
        # 2026-10-06, serg/tasks#1525). Временный каталог убирается сам.
        with _creds_config(creds) as cred_args, \
                tempfile.TemporaryDirectory(prefix="fff_meta_") as meta_cwd:
            proc = subprocess.run(cmd + cred_args + [url],
                                  capture_output=True, text=True, timeout=timeout,
                                  env=_child_env(), cwd=meta_cwd)
    except subprocess.TimeoutExpired:
        # Молчание здесь стоило часов разбора: задание продолжало работу без
        # числа глав, и никто не знал, что метаданные вообще не получены.
        log.warning("метаданные %s не получены за %s с (таймаут)", url, timeout)
        return {}
    return _parse_meta(url, proc.returncode, proc.stdout, proc.stderr)


# --- Драйвер: один процесс FanFicFare на серию ссылок (serg/tasks#1541) ---------
#
# Отдельный процесс на ссылку стоил ~1 с CPU только на старт интерпретатора и
# импорт FanFicFare/cloudscraper, а через FanFicFare в тике идут ~50 подписок.
# Драйвер (fff_meta_driver.py) импортирует всё один раз и на каждое задание
# зовёт `fanficfare.cli.main(argv)` — со СВЕЖИМ Configuration и fetcher, так что
# сессия к DDoS-Guard между ссылками не переиспользуется.

_DRIVER_SCRIPT = Path(__file__).with_name("fff_meta_driver.py")
# Плановый перезапуск: память драйвера растёт от задания к заданию (52→91 МБ RSS
# за 12 вызовов в замере #1525) — каждые N заданий процесс начинается заново.
DRIVER_MAX_JOBS = int(os.environ.get("READER_FFF_DRIVER_MAX_JOBS", "10") or 10)
# Потолок адресного пространства драйвера: утечка превращается в MemoryError
# в драйвере, а не в OOM-kill внутри MemoryMax сервиса (там и uvicorn).
# Замер 06.10: VmPeak 110 МБ к 10-му заданию, поэтому 512 — с запасом впятеро.
DRIVER_AS_MB = int(os.environ.get("READER_FFF_DRIVER_AS_MB", "512") or 0)
# Явная пауза между заданиями одного сайта. Раньше её давал старт процесса
# (~1.5–3 с) плюс sleep 0.25; в одном процессе запросы шли бы впритык, а
# DDoS-Guard ficbook тригеристый. Тратим wall, не CPU.
DRIVER_PAUSE_SEC = {"ficbook.net": 2.5}

_TIMEOUT = object()


class DriverStartError(RuntimeError):
    """Драйвер не поднялся: импорт упал, процесс умер или молчит."""


class MetaDriver:
    """Серия `get_meta` через один долгоживущий процесс FanFicFare.

    Контракт ответа — как у `get_meta`: dict метаданных, `{}` при любом отказе,
    и каждый отказ виден в логе с URL. Зависшее задание (нет строки ответа за
    `timeout`) убивает драйвер; следующее задание поднимает новый. Драйвер,
    который не стартует трижды подряд, выключается до конца серии, и ссылки
    считаются старым путём — отдельным процессом (с warning).

    Использовать как контекстный менеджер: процесс живёт ровно серию.
    """

    def __init__(
        self,
        *,
        max_jobs: int | None = None,
        timeout: int = 120,
        start_timeout: int = 60,
        as_mb: int | None = None,
        pause: dict[str, float] | None = None,
        cmd: list[str] | None = None,
    ) -> None:
        self.max_jobs = max(1, max_jobs or DRIVER_MAX_JOBS)
        self.timeout = timeout
        self.start_timeout = start_timeout
        self.as_mb = DRIVER_AS_MB if as_mb is None else as_mb
        self.pause = dict(DRIVER_PAUSE_SEC if pause is None else pause)
        self._cmd = cmd or [sys.executable, str(_DRIVER_SCRIPT)]
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue | None = None
        self._reader: threading.Thread | None = None
        self._errf = None
        self._cwd: tempfile.TemporaryDirectory | None = None
        self._jobs = 0
        self._seq = 0
        self._rss_kb = 0
        self._start_failures = 0
        self._disabled = False
        self._last_at: dict[str, float] = {}
        self.stats = {"jobs": 0, "starts": 0, "failures": 0, "fallback": 0, "max_rss_kb": 0}

    # -- жизненный цикл --------------------------------------------------------

    def __enter__(self) -> "MetaDriver":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._stop("серия закончена")

    def _start(self) -> None:
        self._cwd = tempfile.TemporaryDirectory(prefix="fff_meta_")
        self._errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        env = _child_env()
        if self.as_mb:
            env["READER_FFF_DRIVER_AS_MB"] = str(self.as_mb)
        self._proc = subprocess.Popen(
            self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._errf,
            text=True, encoding="utf-8", errors="replace", env=env, cwd=self._cwd.name,
        )
        self._lines = queue.Queue()
        self._reader = threading.Thread(
            target=self._pump, args=(self._proc, self._lines), daemon=True, name="fff-driver",
        )
        self._reader.start()
        ready = self._next(self.start_timeout)
        if not isinstance(ready, dict) or not ready.get("ready"):
            why = "молчит" if ready is _TIMEOUT else "умер" if ready is None else f"ответил {ready!r}"
            tail = self._stderr_tail()
            self._stop("не стартовал", kill=True)
            raise DriverStartError(f"драйвер {why} за {self.start_timeout} с; stderr: {tail}")
        self.stats["starts"] += 1
        self._rss_kb = int(ready.get("rss_kb") or 0)
        log.info("fff-драйвер pid=%s запущен (RSS %.0f МБ)", self._proc.pid, self._rss_kb / 1024)

    @staticmethod
    def _pump(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # EOF: процесс закрыл протокол (вышел или умер)

    def _next(self, timeout: float):
        """Следующая строка протокола: dict, None (EOF) или _TIMEOUT."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return _TIMEOUT
        if line is None:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"_garbage": line[:200]}

    def _stderr_tail(self) -> str:
        if self._errf is None:
            return "(нет)"
        try:
            self._errf.flush()
            self._errf.seek(0)
            return _strip_noise(self._errf.read())[-300:] or "(пусто)"
        except (OSError, ValueError) as e:
            return f"(не прочитан: {e})"

    def _stop(self, reason: str, *, kill: bool = False) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if not kill:
                try:
                    proc.stdin.close()
                    proc.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired) as e:
                    log.warning("fff-драйвер pid=%s не вышел сам (%s) — убиваю", proc.pid, e)
                    kill = True
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if self._reader is not None:
                self._reader.join(timeout=2)
            log.info(
                "fff-драйвер pid=%s остановлен (%s): заданий %d, RSS %.0f МБ, код %s",
                proc.pid, reason, self._jobs, self._rss_kb / 1024, proc.returncode,
            )
        if self._errf is not None:
            self._errf.close()
            self._errf = None
        if self._cwd is not None:
            self._cwd.cleanup()
            self._cwd = None
        self._jobs = 0

    # -- задания ---------------------------------------------------------------

    def _pace(self, url: str) -> str | None:
        host = (urlparse(url).hostname or "").lower()
        for site, gap in self.pause.items():
            if host.endswith(site):
                last = self._last_at.get(site)
                if last is not None:
                    wait = gap - (time.monotonic() - last)
                    if wait > 0:
                        time.sleep(wait)
                return site
        return None

    def get_meta(
        self, url: str, *, creds: tuple[str, str] | None = None, extra: dict | None = None,
    ) -> dict:
        site = self._pace(url)
        try:
            return self._get_meta(url, creds, extra)
        finally:
            if site:
                self._last_at[site] = time.monotonic()

    def _fallback(self, url, creds, extra) -> dict:
        self.stats["fallback"] += 1
        return get_meta(url, creds=creds, timeout=self.timeout, extra=extra)

    def _fail(self, url: str, what: str) -> dict:
        self.stats["failures"] += 1
        pid = self._proc.pid if self._proc else None
        tail = self._stderr_tail()
        log.warning("метаданные %s не получены: fff-драйвер pid=%s %s; stderr: %s", url, pid, what, tail)
        self._stop(what, kill=True)
        return {}

    def _get_meta(self, url, creds, extra) -> dict:
        if self._proc is None:
            if self._disabled:
                return self._fallback(url, creds, extra)
            try:
                self._start()
            except (DriverStartError, OSError) as e:
                self._start_failures += 1
                log.warning("fff-драйвер не запустился (%s) — %s считаю отдельным процессом", e, url)
                if self._start_failures >= 3:
                    self._disabled = True
                    log.warning("fff-драйвер выключен до конца серии: %d неудачных стартов подряд",
                                self._start_failures)
                return self._fallback(url, creds, extra)
        self._start_failures = 0
        self._seq += 1
        with _creds_config(creds) as cred_args:
            job = {"id": self._seq, "argv": _meta_args(url, extra) + cred_args + [url]}
            try:
                self._proc.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except OSError as e:
                return self._fail(url, f"не принял задание ({e})")
            res = self._next(self.timeout)
        if res is _TIMEOUT:
            return self._fail(url, f"не ответил за {self.timeout} с (завис), убит")
        if res is None:
            self._proc.poll()
            return self._fail(url, f"умер посреди задания (код {self._proc.returncode})")
        if res.get("id") != self._seq:
            return self._fail(url, f"рассинхрон протокола: {str(res)[:200]}")
        self._jobs += 1
        self.stats["jobs"] += 1
        self._rss_kb = int(res.get("rss_kb") or 0)
        self.stats["max_rss_kb"] = max(self.stats["max_rss_kb"], self._rss_kb)
        meta = _parse_meta(url, res.get("rc"), res.get("stdout"), res.get("stderr"))
        if "MemoryError" in (res.get("error") or ""):
            self._fail(url, "упёрся в RLIMIT_AS")
        elif self._jobs >= self.max_jobs:
            self._stop(f"плановый перезапуск каждые {self.max_jobs}")
        return meta


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
            env=_child_env(),
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
