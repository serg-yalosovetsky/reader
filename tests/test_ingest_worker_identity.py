"""Кто «свой» в очереди скачиваний: юнит воркера по cgroup, а не по INVOCATION_ID.

Живой случай 2026-09-15 (serg/tasks#892, проверка на боевом Postgres до рестарта):
диагностический процесс, запущенный через mesh-ops, получил
WORKER_UNIT = «reader.service@<host>». INVOCATION_ID systemd выставляет ЛЮБОМУ
юниту, в том числе транзиентному юниту mesh-ops, — и такой процесс счёл себя
ридером. Его recover_on_startup вернул бы в очередь ЖИВЫЕ задания прода, и одну
книгу качали бы двое. Юнит берётся из собственной cgroup процесса.
"""

from __future__ import annotations

from backend.app import ingestjob

HOST = "peaceful-albattani"


def test_reader_service_cgroup_is_reader_unit():
    cg = "0::/system.slice/reader.service\n"
    assert ingestjob._detect_unit(cg, HOST, 4242) == f"reader.service@{HOST}"


def test_other_systemd_unit_is_not_mistaken_for_reader():
    cg = "0::/system.slice/mesh-ops-exec-abc123.service\n"
    unit = ingestjob._detect_unit(cg, HOST, 4242)
    assert unit == f"mesh-ops-exec-abc123.service@{HOST}"
    assert not unit.startswith("reader.service")


def test_process_outside_service_unit_is_manual():
    cg = "0::/user.slice/user-0.slice/session-7.scope\n"
    assert ingestjob._detect_unit(cg, HOST, 4242) == f"manual@{HOST}:4242"


def test_unreadable_cgroup_is_manual():
    assert ingestjob._detect_unit("", HOST, 4242) == f"manual@{HOST}:4242"


def test_cgroup_v1_lines_are_understood():
    cg = (
        "12:pids:/system.slice/reader.service\n"
        "11:memory:/system.slice/reader.service\n"
        "1:name=systemd:/system.slice/reader.service\n"
    )
    assert ingestjob._detect_unit(cg, HOST, 1) == f"reader.service@{HOST}"
