"""Обложка вручную: загрузить файл или взять у другой книги (serg/tasks#1055).

Главное, что проверяется: выбор человека не теряется. Такую обложку не заменяют
перекачка, монитор, фоновое обновление и Calibre; её не бракует показ по форме
кадра; неудачная загрузка не портит прежнюю; копия — отдельный файл, поэтому
удаление книги-источника ей не вредит.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session

from backend.app import manual_cover


def _img(w=300, h=450, fmt="PNG", color="#3a7bd5") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, fmt)
    return buf.getvalue()


def _book(client, name: str) -> int:
    r = client.post("/api/library/upload", files={"file": (f"{name}.epub", name.encode() * 4, "application/epub+zip")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _up(client, wid, data, name="c.png", ctype="image/png"):
    return client.post(f"/api/reader/{wid}/cover/upload", files={"file": (name, data, ctype)})


def _work(wid):
    from backend.app.db.models import Work
    from backend.app.db.session import engine

    with Session(engine) as s:
        w = s.get(Work, wid)
        s.expunge(w)
        return w


def _set(wid, **fields):
    from backend.app.db.models import Work
    from backend.app.db.session import engine

    with Session(engine) as s:
        w = s.get(Work, wid)
        for k, v in fields.items():
            setattr(w, k, v)
        s.add(w)
        s.commit()


# --- загрузка -----------------------------------------------------------------


def test_upload_makes_a_manual_cover_that_is_served(client):
    wid = _book(client, "up-serve")
    data = _img()
    r = _up(client, wid, data)
    assert r.status_code == 200, r.text
    assert r.json()["cover_source"] == "manual"
    assert _work(wid).cover_source == "manual"
    got = client.get(f"/api/reader/{wid}/cover")
    assert got.status_code == 200
    assert got.content == data


def test_wide_manual_cover_is_shown_not_rejected_as_a_banner(client):
    """Ширина/высота 4:1 у автоматической обложки отбраковывается как баннер (#984/#997).
    У выбранной человеком — нет, иначе /cover ответил бы 404 на его же картинку."""
    wid = _book(client, "up-wide")
    wide = _img(1200, 300)
    assert _up(client, wid, wide).status_code == 200
    got = client.get(f"/api/reader/{wid}/cover")
    assert got.status_code == 200 and got.content == wide

    from backend.app.routers.reader import _usable_cover

    _set(wid, cover_source="embedded")  # тот же файл, но «автоматический» — баннер
    assert _usable_cover(_work(wid)) is None


@pytest.mark.parametrize("fmt,ctype,name", [("JPEG", "image/jpeg", "a.jpg"), ("WEBP", "image/webp", "a.webp"), ("PNG", "image/png", "a.png")])
def test_three_formats_are_accepted(client, fmt, ctype, name):
    wid = _book(client, f"fmt-{fmt}")
    r = _up(client, wid, _img(fmt=fmt), name, ctype)
    assert r.status_code == 200, r.text
    ext = {"JPEG": ".jpg", "WEBP": ".webp", "PNG": ".png"}[fmt]
    assert Path(_work(wid).cover_path).suffix == ext


def test_type_is_decided_by_content_not_by_extension(client):
    """PNG, присланный как .jpg с image/jpeg, — PNG (расширение файла по содержимому)."""
    wid = _book(client, "spoof-ok")
    assert _up(client, wid, _img(fmt="PNG"), "fake.jpg", "image/jpeg").status_code == 200
    assert Path(_work(wid).cover_path).suffix == ".png"


@pytest.mark.parametrize(
    "data,status",
    [
        pytest.param(b"<html>not an image</html>", 415, id="html"),
        pytest.param(b"GIF89a" + b"\x00" * 64, 415, id="gif-header"),
        pytest.param(_img(fmt="GIF"), 415, id="real-gif"),
        pytest.param(b"", 422, id="empty"),
        pytest.param(_img(50, 50), 422, id="too-small"),  # меньше 100 px
        pytest.param(_img(fmt="JPEG")[:400], 422, id="truncated-jpeg"),
    ],
)
def test_bad_files_are_rejected_and_the_old_cover_stays(client, data, status):
    wid = _book(client, f"bad-{status}-{len(data)}")
    good = _img(color="#00aa00")
    assert _up(client, wid, good).status_code == 200
    before = _work(wid).cover_path
    v_before = client.get("/api/library").json()

    r = _up(client, wid, data, "evil.png", "image/png")
    assert r.status_code == status, r.text

    assert _work(wid).cover_path == before
    assert Path(before).read_bytes() == good
    assert client.get(f"/api/reader/{wid}/cover").content == good
    assert client.get("/api/library").json() == v_before


def test_oversize_file_is_413(client):
    wid = _book(client, "too-big")
    assert _up(client, wid, b"x" * (manual_cover.MAX_BYTES + 1)).status_code == 413


def test_pixel_bomb_is_rejected(client):
    """Мало байт, очень много пикселей: проверка размера — до полного декодирования."""
    wid = _book(client, "bomb")
    bomb = _img(6000, 6000, color="#000000")
    assert len(bomb) < manual_cover.MAX_BYTES
    assert _up(client, wid, bomb).status_code == 413


def test_unknown_book_is_404(client):
    assert _up(client, 999999, _img()).status_code == 404


def test_replacing_removes_the_old_manual_file_and_bumps_cover_v(client):
    wid = _book(client, "replace")
    r1 = _up(client, wid, _img(color="#111111")).json()
    first = _work(wid).cover_path
    r2 = _up(client, wid, _img(color="#eeeeee")).json()
    second = _work(wid).cover_path
    assert first != second
    assert not Path(first).exists() and Path(second).exists()
    assert r2["cover_v"] > r1["cover_v"]  # иначе браузер/sw не перечитают картинку
    listed = {w["id"]: w["cover_v"] for w in client.get("/api/library").json()}
    assert listed[wid] == r2["cover_v"]


def test_uploading_the_same_image_twice_keeps_the_file(client):
    wid = _book(client, "twice")
    data = _img()
    _up(client, wid, data)
    path = _work(wid).cover_path
    assert _up(client, wid, data).status_code == 200
    assert _work(wid).cover_path == path and Path(path).exists()


def test_manual_file_never_collides_with_the_sha1_named_cover(client):
    """<sha1>.<ext> — имя встроенной обложки; ручной файл под ним затёрся бы автоматикой."""
    wid = _book(client, "naming")
    _up(client, wid, _img())
    w = _work(wid)
    assert Path(w.cover_path).name != f"{w.sha1}.png"
    assert manual_cover.is_manual_file(w.cover_path)


def test_replacing_never_deletes_a_foreign_file(client, tmp_path):
    """Прежняя обложка — не наш файл (встроенная <sha1>.jpg): её удалять нельзя."""
    from backend.app.config import COVERS_DIR

    wid = _book(client, "foreign")
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    embedded = COVERS_DIR / f"{_work(wid).sha1}.jpg"
    embedded.write_bytes(_img(fmt="JPEG"))
    _set(wid, cover_path=str(embedded), cover_source="embedded")
    assert _up(client, wid, _img()).status_code == 200
    assert embedded.exists()


# --- копия у другой книги -----------------------------------------------------


def test_copy_from_another_book(client):
    a, b = _book(client, "copy-a"), _book(client, "copy-b")
    data = _img(color="#aa2255")
    _up(client, a, data)
    src_before = _work(a)

    r = client.post(f"/api/reader/{b}/cover/copy-from/{a}")
    assert r.status_code == 200, r.text
    assert r.json()["cover_source"] == "manual"

    wb, wa = _work(b), _work(a)
    assert wb.cover_source == "manual"
    assert wb.cover_path != wa.cover_path  # свой файл, а не общий
    assert Path(wb.cover_path).read_bytes() == data
    # источник не изменился
    assert (wa.cover_path, wa.cover_source, wa.updated_at) == (src_before.cover_path, src_before.cover_source, src_before.updated_at)
    assert client.get(f"/api/reader/{b}/cover").content == data


def test_deleting_the_source_book_does_not_break_the_copy(client):
    a, b = _book(client, "del-a"), _book(client, "del-b")
    data = _img(color="#22aa55")
    _up(client, a, data)
    assert client.post(f"/api/reader/{b}/cover/copy-from/{a}").status_code == 200
    assert client.delete(f"/api/library/{a}").status_code == 200
    got = client.get(f"/api/reader/{b}/cover")
    assert got.status_code == 200 and got.content == data


def test_copy_rejects_self_unknown_and_coverless_source(client):
    a, b = _book(client, "rej-a"), _book(client, "rej-b")
    assert client.post(f"/api/reader/{a}/cover/copy-from/{a}").status_code == 400
    assert client.post(f"/api/reader/{a}/cover/copy-from/999999").status_code == 404
    assert client.post(f"/api/reader/999999/cover/copy-from/{a}").status_code == 404
    _up(client, a, _img(color="#101010"))
    before = _work(a).cover_path
    assert client.post(f"/api/reader/{a}/cover/copy-from/{b}").status_code == 409  # у b обложки нет
    assert _work(a).cover_path == before  # и прежняя обложка цела


# --- автоматика не отбирает выбор человека ------------------------------------


def _png_file(tmp_path, name="new.png") -> Path:
    p = tmp_path / name
    p.write_bytes(_img())
    return p


def test_redownload_does_not_replace_a_manual_cover(client, tmp_path):
    from backend.app.services import _pick_cover

    wid = _book(client, "pick")
    _up(client, wid, _img(color="#777777"))
    new = _png_file(tmp_path)
    assert _pick_cover(new, "embedded", _work(wid)) is None
    _set(wid, cover_source="embedded")  # контроль: у автоматической обложки замена работает
    assert _pick_cover(new, "embedded", _work(wid)) == (str(new), "embedded")


def test_manual_cover_with_missing_file_can_be_replaced(client, tmp_path):
    from backend.app.services import _pick_cover

    wid = _book(client, "pick-gone")
    _up(client, wid, _img())
    Path(_work(wid).cover_path).unlink()
    new = _png_file(tmp_path)
    assert _pick_cover(new, "embedded", _work(wid)) == (str(new), "embedded")


def test_monitor_download_does_not_replace_a_manual_cover(client):
    from backend.accounts.monitor import _apply_at_cover

    wid = _book(client, "monitor")
    _up(client, wid, _img(color="#333333"))
    before = _work(wid).cover_path
    from backend.app.db.session import engine

    with Session(engine) as s:
        from backend.app.db.models import Work

        w = s.get(Work, wid)
        _apply_at_cover(s, w, _img(color="#999999"))
        s.commit()
    assert _work(wid).cover_path == before


def test_background_refresh_skips_manual_covers(client, monkeypatch):
    """Фоновое обновление обложек ходит на сайты за КАЖДОЙ книгой с source_url —
    ручную оно обходить обязано. Контроль: обычную книгу оно берёт."""
    from backend.app.routers import library
    from backend.downloaders import authortoday

    calls: list[str] = []
    monkeypatch.setattr(authortoday, "search_work", lambda title, author="": calls.append(title) or None)

    manual, plain = _book(client, "refresh-manual"), _book(client, "refresh-plain")
    _up(client, manual, _img())
    for wid in (manual, plain):
        _set(wid, source_url="https://ficbook.net/readfic/1")
    library._do_refresh_covers()
    assert calls == ["refresh-plain"]


def test_calibre_cover_fetch_returns_the_manual_file(client):
    from backend.calibre import sync

    wid = _book(client, "calibre")
    wide = _img(1200, 300)
    _up(client, wid, wide)
    _set(wid, site="calibre", calibre_id=4242)
    path = sync.ensure_cover(wid)
    assert path is not None and Path(path).read_bytes() == wide
    assert _work(wid).cover_source == "manual"


def test_explicit_generate_still_replaces_a_manual_cover(client, monkeypatch):
    """Единственное, что заменяет ручную обложку, кроме новой ручной, — явное нажатие
    «Сгенерировать обложку»: это тоже выбор человека, и позже."""
    from backend.app import covers
    from backend.app.config import COVERS_DIR

    wid = _book(client, "generate")
    _up(client, wid, _img())
    gen = COVERS_DIR / "generated-test.png"
    gen.write_bytes(_img(color="#ff00ff"))
    monkeypatch.setattr(covers, "generate_cover", lambda *a, **k: gen)
    r = client.post(f"/api/reader/{wid}/cover/generate?force=1")
    assert r.status_code == 200, r.text
    assert _work(wid).cover_source == "generated"
