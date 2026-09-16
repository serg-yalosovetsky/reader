"""Скрытые книги (serg/tasks#923).

Скрытие — это ВИДИМОСТЬ, а не архив: книга исчезает из сетки библиотеки, но
остаётся в базе со своим файлом, прогрессом и подписками и находится поиском.
Поэтому список отдаёт видимые книги, а скрытые — отдельным запросом
`?hidden=1`: фронт берёт их лениво, только когда человек ищет.
"""

from __future__ import annotations

_UPLOAD = {"file": ("hidden.epub", b"hidden-book-test", "application/epub+zip")}


def _upload(client) -> int:
    r = client.post("/api/library/upload", files=_UPLOAD)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ids(client, hidden: bool = False) -> set[int]:
    r = client.get("/api/library" + ("?hidden=1" if hidden else ""))
    assert r.status_code == 200, r.text
    return {w["id"] for w in r.json()}


def test_book_is_visible_until_hidden(client):
    wid = _upload(client)
    assert wid in _ids(client)
    assert wid not in _ids(client, hidden=True)


def test_hidden_book_leaves_the_list_and_stays_findable(client):
    wid = _upload(client)

    r = client.put(f"/api/library/{wid}/hidden", json={"hidden": True})
    assert r.status_code == 200, r.text
    assert r.json()["hidden"] is True

    # Из обычного списка книга ушла…
    assert wid not in _ids(client)
    # …но осталась в базе и доступна поиску (тот же список с ?hidden=1).
    assert wid in _ids(client, hidden=True)
    # Открыть её по-прежнему можно — скрытие не удаление.
    detail = client.get(f"/api/library/{wid}")
    assert detail.status_code == 200
    assert detail.json()["hidden"] is True


def test_hiding_is_reversible(client):
    wid = _upload(client)
    client.put(f"/api/library/{wid}/hidden", json={"hidden": True})

    r = client.put(f"/api/library/{wid}/hidden", json={"hidden": False})
    assert r.status_code == 200, r.text
    assert r.json()["hidden"] is False
    assert wid in _ids(client)
    assert wid not in _ids(client, hidden=True)


def test_detail_carries_the_flag_for_the_button(client):
    """Кнопка на странице книги должна знать текущее состояние: «Скрыть» или
    «Показать». Пропадёт поле из детали — кнопка молча покажет не то."""
    wid = _upload(client)
    assert client.get(f"/api/library/{wid}").json()["hidden"] is False


def test_unknown_book_is_404(client):
    r = client.put("/api/library/999999/hidden", json={"hidden": True})
    assert r.status_code == 404
