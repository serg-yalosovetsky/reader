"""Unit tests for backend.app.crypto — Fernet encrypt/decrypt round-trip. The key file
lives under the temp READER_SECRET_KEY_PATH set by conftest."""

from backend.app import crypto


def test_encrypt_decrypt_roundtrip():
    secret = "s3cret текст — with unicode"
    token = crypto.encrypt(secret)
    assert token != secret
    assert crypto.decrypt(token) == secret


def test_decrypt_empty_returns_empty():
    assert crypto.decrypt("") == ""


def test_encrypt_is_nondeterministic():
    # Fernet embeds a random IV + timestamp, so two encryptions differ but both decrypt
    a = crypto.encrypt("same")
    b = crypto.encrypt("same")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same"
