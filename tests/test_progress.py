"""Прогресс чтения: контракт с текстовым якорем (text_anchor) — сохранение,
чтение и защита сохранённого якоря от затирания пустым."""

from __future__ import annotations


def _upload(client, seed: str) -> dict:
    # Уникальные байты на тест → уникальный sha1 → отдельный Work (иначе upload
    # дедупит по sha1, и тесты делят одну книгу + прогресс через общую tmp-БД).
    body = f"epub-bytes-{seed}".encode()
    files = {"file": ("t.epub", body, "application/epub+zip")}
    r = client.post("/api/library/upload", files=files)
    assert r.status_code == 200
    return r.json()


def test_progress_roundtrip_text_anchor(client):
    wid = _upload(client, "roundtrip")["id"]
    anchor = "the quick brown fox jumps over"
    r = client.put(
        f"/api/progress/{wid}",
        json={"ratio": 0.5, "locator": "epubcfi(/6/4!/4)", "text_anchor": anchor},
    )
    assert r.status_code == 200
    assert r.json()["text_anchor"] == anchor

    g = client.get(f"/api/progress/{wid}")
    assert g.status_code == 200
    assert g.json()["text_anchor"] == anchor
    assert g.json()["locator"] == "epubcfi(/6/4!/4)"


def test_empty_anchor_does_not_clobber_saved(client):
    wid = _upload(client, "clobber")["id"]
    saved = "saved anchor phrase to keep"
    client.put(f"/api/progress/{wid}", json={"ratio": 0.3, "text_anchor": saved})
    # Релокейт без видимого текста (пустая/картиночная страница) шлёт пустой
    # якорь — он НЕ должен стирать рабочий.
    client.put(f"/api/progress/{wid}", json={"ratio": 0.31, "text_anchor": ""})

    g = client.get(f"/api/progress/{wid}")
    assert g.json()["text_anchor"] == saved
    assert g.json()["ratio"] == 0.31  # доля при этом обновилась


def test_progress_default_has_empty_anchor(client):
    wid = _upload(client, "default")["id"]
    g = client.get(f"/api/progress/{wid}")
    assert g.status_code == 200
    assert g.json()["text_anchor"] == ""
