"""Разовый сбой скачивания не должен хоронить задание (serg/tasks#986).

Живой случай 18.09.2026: Серж добавил тред с forums.sufficientvelocity.com,
скачивание оборвалось на первой же попытке («EPUB не создан»), задание ушло в
error — и всё. Повтор ровно тем же кодом, без единой правки, скачал книгу
целиком: 181 глава за 397 секунд. То есть сбой был разовым, а человеку пришлось
бы нажимать «Добавить» заново, не понимая, есть ли в этом смысл.

У задания есть max_attempts = 3, но фактически тратится одна: `attempts` растёт
при взятии в работу, а вернуть задание в очередь умеет только путь «сервис
упал/перезапущен». Обычная ошибка скачивания повтора не получала.
"""

from __future__ import annotations

from backend.app import ingestjob


def test_first_failure_is_retried():
    """Первая неудача из трёх — задание возвращается в очередь."""
    assert ingestjob._should_retry(attempts=1, max_attempts=3) is True


def test_second_failure_is_retried_too():
    assert ingestjob._should_retry(attempts=2, max_attempts=3) is True


def test_last_attempt_ends_in_error():
    """Попытки исчерпаны — дальше честная ошибка, а не бесконечная карусель."""
    assert ingestjob._should_retry(attempts=3, max_attempts=3) is False


def test_more_attempts_than_allowed_ends_in_error():
    """Защита от рассинхрона счётчиков: больше лимита — тоже конец."""
    assert ingestjob._should_retry(attempts=5, max_attempts=3) is False


def test_single_attempt_setting_is_respected():
    """max_attempts=1 значит «одна попытка» — повторов быть не должно."""
    assert ingestjob._should_retry(attempts=1, max_attempts=1) is False
