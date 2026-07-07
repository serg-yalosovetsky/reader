"""Unit tests for backend.app.storage — format detection, streaming SHA-1, idempotent
import into BOOKS_DIR (pointed at a temp dir by conftest)."""

import hashlib

from backend.app import storage


def test_detect_format():
    assert storage.detect_format("Book.EPUB") == "epub"
    assert storage.detect_format("x.fb2") == "fb2"
    assert storage.detect_format("y.pdf") == "pdf"
    assert storage.detect_format("z.txt") is None
    assert storage.detect_format("noextension") is None


def test_sha1_of_file(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"hello world")
    assert storage.sha1_of_file(p) == hashlib.sha1(b"hello world").hexdigest()


def test_import_file_idempotent(tmp_path):
    src = tmp_path / "book.epub"
    src.write_bytes(b"epub-bytes")
    dest, sha1 = storage.import_file(src)
    assert dest.exists()
    assert dest.name == f"{sha1}.epub"
    # second import of the same content returns the same path, no duplicate
    dest2, sha1b = storage.import_file(src)
    assert dest2 == dest
    assert sha1b == sha1
