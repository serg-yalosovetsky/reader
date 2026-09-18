"""Автоповтор — только для разовых сбоев, не для окончательных отказов (#986).

Первая версия правки #986 возвращала в очередь ЛЮБУЮ неудачу скачивания. Это
сломало два давних теста, и они были правы: «Story does not exist» и
«403 Forbidden» — приговор, а не невезение. Повтор такого через минуту ничего не
меняет, зато человек вместо честной ошибки видит задание, висящее в очереди
несколько минут, и не знает, ждать ему или нет.

Поэтому список повторяемых — БЕЛЫЙ: повторяем только то, что по опыту ридера
бывает разовым (молчаливый отказ FanFicFare из #986, тайм-аут, сетевой сбой,
5xx источника). Всё незнакомое считается окончательным: ошибиться в сторону
честного «error» дешевле, чем гонять карусель повторов по приговору.
"""

from __future__ import annotations

import pytest

from backend.app import ingestjob

RETRYABLE = [
    # Ровно случай #986: FanFicFare молча не создал файл, повтор скачал книгу.
    "Не удалось скачать https://forums.sufficientvelocity.com/threads/x.39492/: "
    "EPUB не создан: FanFicFare промолчал (код возврата 0)",
    "FanFicFare превысил тайм-аут на https://example.org/s/1",
    "readli: сетевая ошибка на https://readli.net/x",
    "author.today: сетевая ошибка на https://author.today/work/1: timeout",
    "searchfloor: скачивание книги вернуло 503",
    "docs.python.org недоступен: ConnectTimeout",
]

FINAL = [
    "Не удалось скачать https://ficbook.net/readfic/1: Story does not exist",
    "Не удалось скачать https://example.org/s/1: 403 Client Error: Forbidden",
    "Не удалось скачать https://example.org/s/1: 404 Client Error: Not Found",
    "Не удалось скачать https://example.org/s/1: Failed to find adapter for URL",
    "нужна http(s)-ссылка",
    # Чужой сбой в коде ридера: повторять нечего, чинить надо трассировку.
    'Traceback (most recent call last):\n  File "x.py"\nRuntimeError: '
    "connection failed for https://***@example.org/s/1",
]


@pytest.mark.parametrize("message", RETRYABLE)
def test_transient_failures_are_retried(message: str):
    assert ingestjob._is_retryable(message) is True, message


@pytest.mark.parametrize("message", FINAL)
def test_final_failures_are_not_retried(message: str):
    assert ingestjob._is_retryable(message) is False, message


def test_empty_message_is_not_retried():
    """Причины нет — значит неизвестно; неизвестное в белый список не входит."""
    assert ingestjob._is_retryable("") is False
    assert ingestjob._is_retryable(None) is False
