"""Контракт списка библиотеки: наружу уходит узкий набор полей.

Список — самый частый ответ сервиса (~1400 записей на каждое открытие), и он
единственный, который читает КАЖДЫЙ клиент. Раньше он материализовал всю модель
Work: наружу ехали sha1, пути файлов на сервере и converted_*, а описания книг
тянулись из базы только чтобы быть выброшенными в Python (33d38ce).

Тест держит две границы сразу:
- внутренние поля не уходят наружу и не раздувают ответ;
- поля, на которых держится интерфейс, из списка не пропадают — «оптимизация»,
  срезавшая updated_at или calibre_id, ломает сортировку и бейджи молча.
"""

from __future__ import annotations

# Внутренние поля модели: пути на диске сервера, хеш файла, служебные флаги
# конвертации, плюс тяжёлое description. В СПИСКЕ им делать нечего.
INTERNAL_FIELDS = {
    "sha1",
    "file_path",
    "converted_path",
    "converted_error",
    "converted_status",
    "cover_path",
    "cover_brief",
    "meta_synced",
    "description",
}

# Без чего список сломается:
#   updated_at   — единственный ключ сортировки библиотеки (spec.reader.update-pipeline);
#   calibre_id   — бейдж Calibre и дедуп калибровых карточек в findLibMatches;
#   cover_v      — версия обложки в URL, без неё обложка не обновится после докачки;
#   series/index — строка серии на карточке и переход «показать всю серию».
REQUIRED_FIELDS = {
    "id",
    "title",
    "author",
    "series",
    "series_index",
    "calibre_id",
    "chapters_count",
    "updated_at",
    "cover_v",
}

# Панель по наведению рисуется из детали книги (frontend/js/library.js →
# showHover → bookPageMeta): бейджи статуса и источника, чипсы жанров, объём.
# В СПИСКЕ этих полей нет намеренно — одни genres весят под 70 КБ на всю
# библиотеку, а показываются для одной книги, на которую навели. Поэтому деталь
# обязана их нести: срежут её — панель молча опустеет, как уже было после
# урезания списка (33d38ce), и заметит это только человек, наведя мышь.
HOVER_FIELDS = {"genres", "rating", "status", "words", "source_url", "site"}

_UPLOAD = {"file": ("contract.epub", b"library-contract-test", "application/epub+zip")}


def _one_book(client) -> dict:
    """Загрузить книгу и вернуть ПОЛНУЮ модель Work, как её отдаёт upload."""
    r = client.post("/api/library/upload", files=_UPLOAD)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_work_model_really_contains_internal_fields(client):
    """Позитивный контроль на живых данных, а не на выдуманном словаре.

    Проверяем, что набор INTERNAL_FIELDS не пустой по отношению к РЕАЛЬНОЙ модели
    Work: upload отдаёт её целиком. Значит вернись список к полной модели —
    test_library_list_hides_internal_fields ниже покраснеет, а не промолчит.
    """
    work = _one_book(client)
    leaked = INTERNAL_FIELDS & set(work)
    assert leaked, (
        "полная модель Work перестала содержать внутренние поля — "
        "тест на утечку больше ничего не проверяет, его надо переписать"
    )


def test_library_list_hides_internal_fields(client):
    _one_book(client)
    items = client.get("/api/library").json()
    assert items, "список пуст — книга не завелась, проверять нечего"
    leaked = INTERNAL_FIELDS & set(items[0])
    assert not leaked, (
        f"список библиотеки отдаёт внутренние поля: {sorted(leaked)}. "
        "Пути на диске сервера и sha1 наружу не отдаются, description — только в детали."
    )


def test_library_list_keeps_fields_the_ui_needs(client):
    _one_book(client)
    items = client.get("/api/library").json()
    missing = REQUIRED_FIELDS - set(items[0])
    assert not missing, (
        f"из списка пропали поля, на которых держится интерфейс: {sorted(missing)}"
    )


def test_library_detail_stays_full(client):
    """Урезан именно СПИСОК. Карточка книги по id обязана остаться полной:
    там показываются описание, полнота и ошибка последней докачки."""
    work = _one_book(client)
    detail = client.get(f"/api/library/{work['id']}").json()
    for field in ("description", "file_format", "source_url"):
        assert field in detail, f"деталь книги потеряла поле {field}"


def test_detail_carries_fields_the_hover_panel_needs(client):
    work = _one_book(client)
    detail = client.get(f"/api/library/{work['id']}").json()
    missing = HOVER_FIELDS - set(detail)
    assert not missing, (
        f"деталь книги потеряла поля, из которых строится панель наведения: "
        f"{sorted(missing)}. Панель не упадёт — она просто перестанет их показывать."
    )


def test_hover_fields_are_absent_from_the_list(client):
    """Обратная сторона того же решения: эти поля НЕ должны вернуться в список.

    Если кто-то починит опустевшую панель, вернув поля в список, тест напомнит,
    что чинить надо дозагрузкой детали: список отдаётся целиком на 1400+ книг.
    """
    _one_book(client)
    items = client.get("/api/library").json()
    leaked = HOVER_FIELDS & set(items[0])
    assert not leaked, (
        f"в списке снова появились поля панели: {sorted(leaked)}. "
        "Панель дозагружает деталь по одной книге — раздувать список для этого не нужно."
    )
