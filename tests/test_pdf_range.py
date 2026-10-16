"""Исходный PDF: ревизия доступна без чтения тела, Range остаётся частичным."""
import hashlib


def test_pdf_range_and_head(client):
    data = b'%PDF-1.4\n' + b'x' * 200000
    uploaded = client.post('/api/library/upload', files={'file': ('range.pdf', data, 'application/pdf')})
    assert uploaded.status_code == 200, uploaded.text
    url = f"/api/reader/{uploaded.json()['id']}/file?original=1"
    rev = hashlib.sha1(data).hexdigest()
    head = client.head(url)
    assert head.status_code == 200
    assert not head.content
    assert head.headers['x-book-sha1'] == rev
    assert int(head.headers['content-length']) == len(data)
    response = client.get(url, headers={'Range': 'bytes=0-65535'})
    assert response.status_code == 206
    assert response.content == data[:65536]
    assert response.headers['content-range'] == f'bytes 0-65535/{len(data)}'
    assert response.headers['x-book-sha1'] == rev
    assert response.headers['etag'] == head.headers['etag']
    unchanged = client.get(url, headers={'If-None-Match': head.headers['etag']})
    assert unchanged.status_code == 304
    assert unchanged.headers['x-book-sha1'] == rev
