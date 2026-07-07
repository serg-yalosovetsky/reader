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
