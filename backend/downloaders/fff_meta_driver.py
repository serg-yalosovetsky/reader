"""Драйвер `fanficfare --json-meta` для фазы счёта глав монитора (serg/tasks#1541).

Один процесс на много ссылок вместо процесса на ссылку: старт интерпретатора и
импорт FanFicFare/cloudscraper стоили ~1 с CPU на КАЖДУЮ из ~50 подписок тика.

Запускается ОТДЕЛЬНЫМ скриптом (`python fff_meta_driver.py`), а не как модуль
пакета backend: пакет тянет за собой сервис. Родитель — `fanficfare_engine.MetaDriver`,
он же даёт очищенное окружение (`_child_env`) и временный cwd. Протокол построчный:

    stdout: {"ready": true, "pid": N}                  — один раз, импорт прошёл
    stdin : {"id": N, "argv": [...]}                   — одно задание = одна строка
    stdout: {"id": N, "rc": int, "stdout": str, "stderr": str, "error": str, "rss_kb": int}

`argv` — ровно те аргументы, с которыми `get_meta` запускает отдельный процесс.
Каждое задание — новый вызов `fanficfare.cli.main(argv)`: он сам создаёт новые
options, Configuration и fetcher (cli.do_download → get_configuration, fetcher
живёт в Configuration), поэтому сессия cloudscraper и куки между ссылками НЕ
переиспользуются. Это обязательно: DDoS-Guard ficbook банит переиспользуемую
сессию после ~десятка запросов.

Протокольные потоки отделены от потоков FanFicFare на уровне дескрипторов:
протокол идёт через копии fd 0/1, а сами fd 0/1 указывают на /dev/null и stderr.
Так ни `print`, ни случайное чтение stdin внутри FanFicFare (интерактивные
вопросы) не попадут в протокол и не съедят следующее задание.

Память ограничена RLIMIT_AS (`READER_FFF_DRIVER_AS_MB`): при утечке MemoryError
получает этот процесс, а не OOM-killer выбирает uvicorn внутри MemoryMax сервиса.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import traceback


def _limit_memory() -> None:
    mb = int(os.environ.get("READER_FFF_DRIVER_AS_MB", "0") or 0)
    if mb <= 0:
        return
    import resource

    limit = mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _rss_kb() -> int:
    """Текущий RSS процесса, КБ (0 там, где /proc нет — например, Windows)."""
    try:
        with open("/proc/self/statm", encoding="ascii") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") // 1024
    except (OSError, ValueError, IndexError, AttributeError):
        # Размер — справочная цифра для лога родителя; 0 он показывает как «нет данных».
        return 0


@contextlib.contextmanager
def _capture_logging(buf: io.StringIO):
    """Логгер `fanficfare` держит поток stderr, взятый при импорте; на время
    задания подменяем его на буфер, иначе предупреждения шли бы мимо ответа."""
    swapped = []
    for name in ("", "fanficfare"):
        for h in logging.getLogger(name).handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                swapped.append((h, h.setStream(buf)))
    try:
        yield
    finally:
        for h, old in swapped:
            h.setStream(old)


def _run(main, argv: list[str]) -> dict:
    out, err = io.StringIO(), io.StringIO()
    rc, error = 0, ""
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), _capture_logging(err):
        try:
            main(argv)
        except SystemExit as e:
            code = e.code
            rc = code if isinstance(code, int) else (0 if code is None else 1)
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 — причина уходит родителю в ответе (поле error)
            rc = 1
            error = f"{type(e).__name__}: {e}"
            err.write(traceback.format_exc(limit=3))
    return {"rc": rc, "stdout": out.getvalue(), "stderr": err.getvalue(), "error": error}


def _reply(proto, res: dict) -> None:
    proto.write(json.dumps(res, ensure_ascii=False) + "\n")
    proto.flush()


def serve(main, stdin, proto) -> int:
    """Цикл заданий. Возвращает код выхода процесса."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
            argv = [str(a) for a in job["argv"]]
        except (ValueError, KeyError, TypeError) as e:
            _reply(proto, {"id": None, "rc": 2, "stdout": "", "stderr": "",
                           "error": f"плохое задание: {e}", "rss_kb": _rss_kb()})
            continue
        try:
            res = _run(main, argv)
        except MemoryError:
            # Процесс с исчерпанной памятью дальше не живёт: ответ и выход,
            # родитель поднимет свежий драйвер.
            _reply(proto, {"id": job.get("id"), "rc": 1, "stdout": "", "stderr": "",
                           "error": "MemoryError: драйвер упёрся в RLIMIT_AS",
                           "rss_kb": _rss_kb()})
            return 3
        res["id"] = job.get("id")
        res["rss_kb"] = _rss_kb()
        _reply(proto, res)
    return 0


def _detach_std_streams():
    """Протокол — на копиях fd 0/1; сами fd 0/1 — /dev/null и stderr."""
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115 — живёт до конца процесса
    return proto_in, proto_out


def _main() -> int:
    _limit_memory()
    proto_in, proto_out = _detach_std_streams()
    from fanficfare.cli import main as fff_main

    # Сообщить родителю, что импорт прошёл: иначе сломанный драйвер неотличим
    # от медленного первого задания.
    _reply(proto_out, {"ready": True, "pid": os.getpid(), "rss_kb": _rss_kb()})
    return serve(fff_main, proto_in, proto_out)


if __name__ == "__main__":
    sys.exit(_main())
