"""Отказ скачивания обязан объяснять себя (serg/tasks#986).

Живой случай 18.09.2026: Серж добавлял тред с forums.sufficientvelocity.com и
получил «Не удалось скачать …: EPUB не создан». Больше не было НИЧЕГО — ни в
интерфейсе, ни в логе: FanFicFare завершился, файла нет, оба потока пусты, а код
возврата процесса код ридера получал и выбрасывал (`_rc, out, err = ...`).

Разбор занял часы и показал, что сам сбой был разовым (повтор тем же кодом
скачал книгу целиком, 181 глава). То есть цена немого сообщения — не «неудобно»,
а «человек не знает, повторить ему или это навсегда сломано».

Правило: причина отказа не может быть пустой. Нет текста от FanFicFare — значит
в сообщение идёт код возврата и признание, что загрузчик промолчал.
"""

from __future__ import annotations

from backend.downloaders import fanficfare_engine as ffe


def test_silent_failure_names_the_exit_code():
    """Оба потока пусты — человек всё равно должен узнать код возврата."""
    msg = ffe._failure_reason(1, "", "")
    assert "1" in msg, msg
    assert msg.strip(), "пустая причина: ровно то, из-за чего задача и появилась"


def test_silent_success_code_is_still_reported():
    """Худший случай: код 0, файла нет, вывода нет — молчание надо назвать вслух."""
    msg = ffe._failure_reason(0, "", "")
    assert "0" in msg, msg
    assert len(msg) > 10, msg


def test_real_reason_wins_over_the_code():
    """Есть настоящая причина — показываем её, а не код возврата."""
    msg = ffe._failure_reason(1, "Story does not exist", "")
    assert "Story does not exist" in msg


def test_noise_does_not_hide_the_reason():
    """Служебная строка venv не должна вытеснять причину из другого потока."""
    msg = ffe._failure_reason(
        1, "Story does not exist", "[sentry_bootstrap] Sentry initialized"
    )
    assert "Story does not exist" in msg
    assert "sentry_bootstrap" not in msg


def test_noise_only_output_still_gives_something_useful():
    """Если кроме служебного шума ничего нет — сообщение всё равно осмысленное."""
    msg = ffe._failure_reason(2, "", "[sentry_bootstrap] Sentry initialized")
    assert "2" in msg, msg
    assert msg.strip()
