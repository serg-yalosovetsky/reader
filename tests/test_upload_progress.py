"""Файлы используют задания и статусы общей панели скачиваний (#1904)."""
from backend.app import upload_progress
from backend.app.routers import library


def test_upload_stages_and_result(client, monkeypatch):
    stages = []
    original = upload_progress.update

    def record(job_id, stage, **kw):
        stages.append(stage)
        return original(job_id, stage, **kw)

    monkeypatch.setattr(upload_progress, "update", record)
    monkeypatch.setattr(library.covers, "extract_cover", lambda *args: None)
    job = client.post('/api/library/upload/jobs', json={'filename': 'book.fb2'}).json()
    assert job['kind'] == 'upload'
    response = client.post('/api/library/upload', params={'job_id': job['job_id']},
                           files={'file': ('book.fb2', b'<FictionBook><description><title-info><book-title>Test upload</book-title></title-info></description></FictionBook>')})
    assert response.status_code == 200, response.text
    status = client.get('/api/ingest/jobs/' + job['job_id']).json()
    assert status['status'] == 'done'
    assert status['work']['title'] == response.json()['title']
    assert stages == ['приём файла', 'обработка файла', 'извлечение метаданных', 'извлечение обложки', 'добавлено']
    assert any(j['job_id'] == job['job_id'] for j in client.get('/api/ingest/jobs').json()['jobs'])


def test_upload_failure_is_visible(client):
    job = client.post('/api/library/upload/jobs', json={'filename': 'bad.zip'}).json()
    response = client.post('/api/library/upload', params={'job_id': job['job_id']},
                           files={'file': ('bad.zip', b'not zip')})
    assert response.status_code == 400
    status = client.get('/api/ingest/jobs/' + job['job_id']).json()
    assert status['status'] == 'error'
    assert status['error'] == response.json()['detail']


def test_multiple_uploads_are_distinct(client):
    jobs = [client.post('/api/library/upload/jobs', json={'filename': 'same.pdf'}).json() for _ in range(2)]
    assert jobs[0]['job_id'] != jobs[1]['job_id']
    for job in jobs:
        response = client.post('/api/library/upload', params={'job_id': job['job_id']}, files={'file': ('bad.txt', b'bad')})
        assert response.status_code == 400
        assert client.get('/api/ingest/jobs/' + job['job_id']).json()['status'] == 'error'


def test_upload_job_lifecycle_without_http():
    from backend.app import ingestjob
    from backend.app.db.session import init_db
    init_db()
    job = upload_progress.create("пример.pdf")
    assert job["kind"] == "upload" and job["progress"]["stage"] == "приём файла"
    upload_progress.update(job["job_id"], "извлечение обложки")
    assert ingestjob.get(job["job_id"])["progress"]["stage"] == "извлечение обложки"
    upload_progress.update(job["job_id"], "добавлено", works=[{"id": 123, "title": "Пример"}])
    status = ingestjob.get(job["job_id"])
    assert status["status"] == "done"
    assert status["work"] == {"id": 123, "title": "Пример", "author": "", "chapters": 0}


def test_upload_error_without_http():
    from backend.app import ingestjob
    from backend.app.db.session import init_db
    init_db()
    job = upload_progress.create("плохой.zip")
    upload_progress.update(job["job_id"], "ошибка загрузки", error="файл не открывается как zip-архив")
    status = ingestjob.get(job["job_id"])
    assert status["status"] == "error"
    assert status["error"] == "файл не открывается как zip-архив"
    assert status["finished_at"] is not None


def test_upload_restart_does_not_download_filename():
    from backend.app import ingestjob
    from backend.app.db.session import init_db
    init_db()
    job = upload_progress.create("локальный.pdf")
    ingestjob.mark_shutdown()
    status = ingestjob.get(job["job_id"])
    assert status["status"] == "error"
    assert "отправьте файл заново" in status["error"]


def test_same_filename_can_upload_in_parallel():
    from backend.app.db.session import init_db
    init_db()
    first = upload_progress.create("same.pdf")
    second = upload_progress.create("same.pdf")
    assert first["job_id"] != second["job_id"]
    upload_progress.update(first["job_id"], "ошибка", error="отмена")
    upload_progress.update(second["job_id"], "ошибка", error="отмена")


def test_zip_reports_all_book_titles(client, monkeypatch):
    import io
    import zipfile
    monkeypatch.setattr(library.covers, "extract_cover", lambda *args: None)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for title in ("Первая", "Вторая"):
            archive.writestr(title + ".fb2", f"<FictionBook><description><title-info><book-title>{title}</book-title></title-info></description></FictionBook>")
    job = client.post('/api/library/upload/jobs', json={'filename': 'books.zip'}).json()
    result = client.post('/api/library/upload', params={'job_id': job['job_id']}, files={'file': ('books.zip', content.getvalue())})
    assert result.status_code == 200, result.text
    assert result.json()['added'] == 2
    status = client.get('/api/ingest/jobs/' + job['job_id']).json()
    assert status['status'] == 'done'
    assert 'Первая' in status['title'] and 'Вторая' in status['title']
