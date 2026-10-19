"""Перепаковка: python -m backend.scripts.embed_remote_images [--apply].

По умолчанию только инвентаризация, без сети и записи. Запускать при
остановленных загрузчиках, чтобы они не заменили книгу во время перепаковки.
"""
from __future__ import annotations

import argparse
import logging
import os
import posixpath
import re
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

import httpx
from defusedxml import ElementTree as ET
from sqlmodel import Session, select

from backend.app import config
from backend.app.db.models import Work, utcnow
from backend.app.db.session import engine
from backend.app.storage import sha1_of_file
from backend.downloaders.authortoday import _get
from backend.downloaders.remote_images import ImageEmbedder, remote_urls

logger = logging.getLogger(__name__)
OPF = 'http://www.idpf.org/2007/opf'


def repack(path: Path, client, work_id, apply=False) -> Path | None:
    """Новый файл <sha1>.epub рядом со старым (как у обычного обновления книги) или None."""
    with ZipFile(path) as archive:
        documents = {name: archive.read(name).decode('utf-8') for name in archive.namelist()
                     if name.lower().endswith(('.xhtml', '.html', '.htm'))}
        urls = set().union(*(remote_urls(text) for text in documents.values()))
        if not urls:
            return None
        print(f'{path}: {len(urls)} внешних картинок' + (' — встраивание' if apply else ' — скачал бы и перепаковал'))
        if not apply:
            return None
        container = ET.fromstring(archive.read('META-INF/container.xml'))
        opf_path = next(el.attrib['full-path'] for el in container.iter()
                        if el.tag.endswith('}rootfile'))
        package = ET.fromstring(archive.read(opf_path))
        manifest = package.find(f'{{{OPF}}}manifest')
        if manifest is None:
            raise ValueError(f'{path}: нет OPF manifest')
        base = posixpath.dirname(opf_path)
        prefix = posixpath.join(base, 'remote-images')
        # Свободный каталог не перезаписывает иллюстрации исходного EPUB.
        while any(name.startswith(prefix + '/') for name in archive.namelist()):
            prefix += '-new'
        embedder = ImageEmbedder(lambda url, limit: _get(client, url, max_bytes=limit), work_id, prefix)
        rewritten = {name: embedder.rewrite(text, lambda image, name=name:
                     posixpath.relpath(image, posixpath.dirname(name) or '.')).encode('utf-8')
                     for name, text in documents.items()}
        if not embedder.images:
            return None
        ids = {item.get('id') for item in manifest}
        items = []
        for n, (name, _, mime) in enumerate(embedder.images, 1):
            uid = f'remote-image-{n}'
            while uid in ids:
                uid += '-new'
            ids.add(uid)
            href = escape(posixpath.relpath(name, base or '.'), {'"': '&quot;'})
            items.append(f'<item id="{uid}" href="{href}" media-type="{mime}"/>')
        # Вставка текстом: пересборка ElementTree переименовала бы префиксы (ns0:package).
        opf = archive.read(opf_path).decode('utf-8')
        end = re.search(r'</(?:[\w-]+:)?manifest\s*>', opf)
        if not end:
            raise ValueError(f'{path}: нет закрывающего </manifest>')
        rewritten[opf_path] = (opf[:end.start()] + ''.join(items) + opf[end.start():]).encode('utf-8')
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                with ZipFile(output, 'w') as dest:
                    for info in archive.infolist():
                        dest.writestr(info, rewritten.get(info.filename, archive.read(info.filename)))
                    for name, data, _ in embedder.images:
                        dest.writestr(name, data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, path.stat().st_mode)
            # Имя по SHA-1 содержимого, как в storage.import_file: старый файл не трогаем,
            # пока БД не переключена на новый.
            dest = path.with_name(sha1_of_file(Path(temporary)) + path.suffix)
            os.replace(temporary, dest)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return dest


def update_work(session, old: Path, new: Path):
    """Переключить книги со старого файла на новый, затем удалить старый."""
    sha1 = sha1_of_file(new)
    switched = 0
    for work in session.exec(select(Work)).all():
        if work.file_path and Path(work.file_path).resolve() == old.resolve():
            work.file_path = str(new)
            work.sha1 = sha1
            work.content_updated_at = utcnow()
            session.add(work)
            switched += 1
        elif work.converted_path and Path(work.converted_path).resolve() == old.resolve():
            work.converted_path = str(new)
            work.content_updated_at = utcnow()
            session.add(work)
            switched += 1
    session.commit()
    if not switched:
        # Файл без книги в БД: перепакованная копия остаётся рядом, исходник не трогаем.
        logger.warning('%s: ни одна книга в БД не ссылается на файл; оставлены оба (%s)', old, new.name)
        return
    # Сбой до commit оставит лишний новый файл, но не книгу без файла.
    old.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    failures = 0
    with Session(engine) as session, httpx.Client(follow_redirects=True) as client:
        work_ids = {}
        if args.apply:
            for work in session.exec(select(Work)).all():
                for filename in (work.file_path, work.converted_path):
                    if filename:
                        work_ids[Path(filename).resolve()] = work.id
        for path in sorted(config.BOOKS_DIR.rglob('*')):
            if path.suffix.lower() != '.epub':
                continue
            try:
                new = repack(path, client, work_ids.get(path.resolve(), str(path)), args.apply)
                if new:
                    update_work(session, path, new)
            except Exception:
                session.rollback()
                failures += 1
                logger.exception('Не удалось перепаковать %s или обновить БД', path)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
