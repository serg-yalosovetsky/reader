"""API smoke tests via TestClient against an in-memory DB — exercise app boot,
middleware, router wiring and the read endpoints on an empty database."""


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_library_list_empty(client):
    r = client.get("/api/library")
    assert r.status_code == 200
    r.json()  # parseable


def test_progress_list_empty(client):
    r = client.get("/api/progress")
    assert r.status_code == 200
    r.json()


def test_bookmarks_unknown_work_is_empty(client):
    r = client.get("/api/bookmarks/999999")  # work_id is an int
    assert r.status_code == 200
    assert r.json() == []


def test_highlights_unknown_work_is_empty(client):
    r = client.get("/api/highlights/999999")
    assert r.status_code == 200
    assert r.json() == []


def test_bookmarks_bad_work_id_is_422(client):
    # non-int work_id is rejected by FastAPI validation
    assert client.get("/api/bookmarks/not-an-int").status_code == 422


def test_upload_epub_stores_work(client):
    # exercises the async anyio file-write path in upload_book + sha1 + import
    files = {
        "file": (
            "tiny.epub",
            b"not-a-real-epub-but-stored-by-sha1",
            "application/epub+zip",
        )
    }
    r = client.post("/api/library/upload", files=files)
    assert r.status_code == 200
    w = r.json()
    assert w["file_format"] == "epub"
    assert w["sha1"]
    # re-upload of identical bytes is deduped to the same work (same sha1)
    r2 = client.post("/api/library/upload", files=files)
    assert r2.status_code == 200
    assert r2.json()["sha1"] == w["sha1"]


def test_upload_rejects_unsupported_format(client):
    files = {"file": ("note.txt", b"hello", "text/plain")}
    assert client.post("/api/library/upload", files=files).status_code == 400


def test_rename_book(client):
    from backend.app.db.models import Work
    from backend.app.db.session import engine
    from backend.app.bookmeta import apply_meta
    from sqlmodel import Session

    w = client.post('/api/library/upload', files={
        'file': ('rename.epub', b'book-for-renaming', 'application/epub+zip'),
    }).json()
    url = f"/api/library/{w['id']}/identity"
    for payload in ({'title': '  '}, {'title': 'a' * 501}, {'title': None},
                    {'title': 'Book', 'author': 'a' * 501}):
        assert client.put(url, json=payload).status_code == 422
    r = client.put(url, json={'title': ' Refactoring UI ', 'author': ' Adam Wathan & Steve Schoger '})
    assert r.status_code == 200
    assert r.json()['title'] == 'Refactoring UI'
    assert r.json()['author'] == 'Adam Wathan & Steve Schoger'
    with Session(engine) as s:
        book = s.get(Work, w['id'])
        assert book.title_manual and book.author_manual
        assert not apply_meta(book, {'title': 'Automatic', 'author': 'Automatic'}, overwrite=True)
        assert book.title == 'Refactoring UI'
    assert client.put(url, json={'title': 'New title'}).json()['author'] == 'Adam Wathan & Steve Schoger'
    assert client.put(url, json={'title': 'New title', 'author': ''}).json()['author'] == ''
    assert client.get(f"/api/library/{w['id']}").json()['title'] == 'New title'
    assert client.put('/api/library/999999/identity', json={'title': 'Book'}).status_code == 404


def test_manual_identity_columns_migrate(monkeypatch):
    from sqlalchemy import text
    from sqlmodel import create_engine
    from backend.app.db import session as db

    old_engine = create_engine('sqlite://')
    with old_engine.begin() as connection:
        connection.execute(text('CREATE TABLE work (id INTEGER PRIMARY KEY, title VARCHAR)'))
        connection.execute(text("INSERT INTO work (id, title) VALUES (1, 'Старая книга')"))
    monkeypatch.setattr(db, 'engine', old_engine)
    added = db._migrate_add_columns()
    assert ('work', 'title_manual') in added
    assert ('work', 'author_manual') in added
    with old_engine.connect() as connection:
        assert connection.execute(text('SELECT title_manual, author_manual FROM work')).one() == (0, 0)
    assert not db._migrate_add_columns()
