#!/usr/bin/env python3
"""Import new books from the Syncthing-synced /srv/books folder into the reader.

Local-folder analogue of scan-drive-books. Idempotent: the reader's /upload
dedups by SHA-1 (returns the existing Work), and we also skip files unchanged
since last run via a small state file. `.fb2.zip` (and other zips) are unpacked
to their inner .fb2/.epub/.pdf before upload. Run by a systemd timer; manual:

    scan_local_books.py
"""
import json
import pathlib
import subprocess
import tempfile
import zipfile

BOOKS_DIR = pathlib.Path("/srv/books")
UPLOAD_URL = "http://127.0.0.1:8123/api/library/upload"
STATE = pathlib.Path("/root/reader/data/.local_books_seen.json")
BOOK_EXT = {".epub", ".fb2", ".pdf"}


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def upload(path: pathlib.Path) -> str:
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", UPLOAD_URL, "-F", f"file=@{path}"],
        capture_output=True, text=True, timeout=180,
    )
    return r.stdout.strip()


def inner_book(zpath: pathlib.Path, tmpdir: str) -> pathlib.Path | None:
    with zipfile.ZipFile(zpath) as z:
        members = [n for n in z.namelist() if pathlib.Path(n).suffix.lower() in BOOK_EXT]
        if not members:
            return None
        out = pathlib.Path(tmpdir) / pathlib.Path(members[0]).name
        out.write_bytes(z.read(members[0]))
        return out


def main() -> None:
    if not BOOKS_DIR.is_dir():
        print("no /srv/books"); return
    state = load_state()
    imported, skipped, errors = [], 0, []

    for p in sorted(BOOKS_DIR.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        suf = p.suffix.lower()
        if suf not in BOOK_EXT and suf != ".zip":
            continue
        key = str(p)
        sig = f"{p.stat().st_size}:{int(p.stat().st_mtime)}"
        if state.get(key) == sig:
            skipped += 1
            continue
        try:
            if suf == ".zip":
                with tempfile.TemporaryDirectory() as td:
                    book = inner_book(p, td)
                    if not book:
                        print(f"skip (no book inside): {p.name}")
                        state[key] = sig
                        continue
                    out = upload(book)
            else:
                out = upload(p)
            print(f"imported {p.name}: {out[:140]}")
            imported.append(p.name)
            state[key] = sig
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p.name}: {e}")
            print(f"error {p.name}: {e}")

    STATE.write_text(json.dumps(state))
    print(json.dumps({"imported": imported, "skipped": skipped, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
