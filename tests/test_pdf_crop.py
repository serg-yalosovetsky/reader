"""Серверные поля PDF и смена редакции кэша."""
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from backend.app import pdf_crop


def test_boxes():
    blank = Image.new('L', (200, 300), 255)
    assert pdf_crop.content_box(blank) is None
    ImageDraw.Draw(blank).rectangle((40, 60, 159, 239), fill=0)
    box = pdf_crop.content_box(blank)
    assert box == pytest.approx([.185, .19, .815, .81])
    assert pdf_crop.stable_box([None, [0, 0, 1, 1], box]) == box
    assert pdf_crop.stable_box([None]) == [0, 0, 1, 1]


def test_calculate_batches(tmp_path, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs['timeout'] > 0
        if args[0] == 'pdfinfo':
            return subprocess.CompletedProcess(args, 0, stdout='Pages: 11\n')
        first, last = int(args[args.index('-f')+1]), int(args[args.index('-l')+1])
        for page in range(first, last+1):
            image = Image.new('L', (100, 150), 255)
            if page != 2:
                ImageDraw.Draw(image).rectangle((20, 30, 79, 119), fill=0)
            image.save(f'{args[-1]}-{page:02}.pgm')
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(pdf_crop.subprocess, 'run', run)
    path = tmp_path / 'book.pdf'
    result = pdf_crop.calculate(path, 'one')
    assert len(result['pages']) == 11
    assert result['pages'][1] is None
    assert len(calls) == 3
    assert json.loads(pdf_crop.cache_path(path, 'one').read_text()) == result
    assert not pdf_crop.cache_path(path, 'two').exists()
    assert pdf_crop.ensure(path, 'one') == result
    pdf_crop.calculate(path, 'two')
    assert pdf_crop.cache_path(path, 'two').exists()


def test_endpoint(client, monkeypatch):
    monkeypatch.setattr(pdf_crop, 'ensure', lambda *args: {'status':'pending', 'book':None, 'pages':[]})
    response = client.post('/api/library/upload', files={'file': ('crop.pdf', b'%PDF-1.4\ncrop', 'application/pdf')})
    url = f"/api/reader/{response.json()['id']}/pdf-crop"
    pending = client.get(url)
    assert pending.json()['status'] == 'pending'
    assert pending.headers['cache-control'] == 'no-store'
    ready = {'status':'ready', 'book':[.1,.1,.9,.9], 'pages':[None]}
    monkeypatch.setattr(pdf_crop, 'ensure', lambda *args: ready)
    response = client.get(url)
    assert response.json() == ready
    etag = response.headers['etag']
    assert client.get(url, headers={'If-None-Match':etag}).status_code == 304
    from backend.app.db.session import engine
    from backend.app.db.models import Work
    from sqlmodel import Session
    with Session(engine) as session:
        work = session.get(Work, int(url.split('/')[3]))
        work.sha1 = 'changed'
        session.add(work)
        session.commit()
    assert client.get(url).headers['etag'] != etag
