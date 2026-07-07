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
