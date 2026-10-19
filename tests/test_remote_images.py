"""Встраивание иллюстраций без настоящей сети."""
import socket
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest
from defusedxml import ElementTree as ET
from PIL import Image

from backend.app.storage import sha1_of_file
from backend.downloaders import authortoday, remote_images
from backend.downloaders.remote_images import ImageEmbedder, fetch_image, image_client
from backend.scripts.embed_remote_images import repack, update_work


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    # Ни один тест не обращается к настоящему DNS.
    monkeypatch.setattr(remote_images.socket, 'getaddrinfo', lambda *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])


@pytest.fixture
def png():
    out = BytesIO()
    Image.new('RGB', (2, 2)).save(out, format='PNG')
    return out.getvalue()


def test_rewrite_deduplicate_and_epub(png):
    calls = []

    def fetch(url, limit):
        calls.append(url)
        return png

    embed = ImageEmbedder(fetch, '4657')
    body = embed.rewrite('<p><img width="20" src="https://host/a?a=1&amp;b=2"/></p>')
    assert 'src="images/i0001.png"' in body
    assert 'width="20"' in body
    assert 'images/i0001.png' in embed.rewrite("<img src='https://host/a?a=1&amp;b=2'>")
    assert calls == ['https://host/a?a=1&b=2']
    assert len(embed.images) == 1
    path = authortoday._build_epub('1', 'Книга', '', '', [('Глава', body)], images=embed.images)
    with ZipFile(path) as archive:
        assert archive.read('EPUB/images/i0001.png') == png
        assert b'images/i0001.png' in archive.read('EPUB/chap_1.xhtml')
        assert b'image/png' in archive.read('EPUB/content.opf')


def test_failure_is_logged_and_not_retried_per_occurrence(caplog):
    calls = []

    def fetch(url, limit):
        calls.append(url)
        raise httpx.ReadTimeout('timeout')

    embed = ImageEmbedder(fetch, '4657')
    markup = '<img src="https://host/fail"><img src="https://host/fail">'
    assert embed.rewrite(markup) == markup
    assert calls == ['https://host/fail']
    assert '4657' in caplog.text and 'https://host/fail' in caplog.text
    assert not embed.images


def test_budget_and_bad_image(png, monkeypatch, caplog):
    from backend.downloaders import remote_images
    monkeypatch.setattr(remote_images, 'MAX_BOOK', len(png))
    embed = ImageEmbedder(lambda url, limit: png, '1')
    assert 'images/i0001.png' in embed.rewrite('<img src=https://host/one>')
    assert 'src=https://host/two' in embed.rewrite('<img src=https://host/two>')
    bad = ImageEmbedder(lambda url, limit: b'not an image', '2')
    assert bad.rewrite('<img src="https://host/bad">') == '<img src="https://host/bad">'
    assert 'https://host/bad' in caplog.text


def test_stream_limit_and_book_get_retry(png, monkeypatch):
    calls = []

    def serve(request):
        calls.append(request.url)
        if len(calls) == 1:
            raise httpx.ConnectError('offline')
        return httpx.Response(200, content=png)

    monkeypatch.setattr(authortoday.time, 'sleep', lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        assert authortoday._get(client, 'https://host/a').content == png
        assert len(calls) == 2
        with pytest.raises(ValueError, match='лимит'):
            fetch_image(client, 'https://host/a', 1)


def test_repack_dry_run_apply_and_db(tmp_path, png, session, monkeypatch):
    from backend.app.db.models import Work
    path = tmp_path / 'book.epub'
    with ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
        archive.writestr('OPS/book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="c" href="text/c.xhtml" media-type="application/xhtml+xml"/></manifest></package>')
        archive.writestr('OPS/text/c.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="https://host/a"/></body></html>')
    original = path.read_bytes()
    calls = []

    def serve(request):
        calls.append(request.url)
        return httpx.Response(200, content=png)

    work = Work(file_path=str(path), sha1='old')
    session.add(work)
    session.commit()
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        assert not repack(path, client, work.id)
        assert not calls and path.read_bytes() == original
        # Ошибка финальной замены не повреждает исходную книгу и убирает tmp.
        with monkeypatch.context() as patch:
            def fail_replace(*args):
                raise OSError('replace failed')
            patch.setattr('backend.scripts.embed_remote_images.os.replace', fail_replace)
            with pytest.raises(OSError, match='replace failed'):
                repack(path, client, work.id, apply=True)
        assert path.read_bytes() == original
        assert not list(tmp_path.glob('*.tmp'))
        new = repack(path, client, work.id, apply=True)
        assert new and new.name == sha1_of_file(new) + '.epub'
        assert path.read_bytes() == original  # старый файл живёт, пока БД не переключена
        assert not repack(new, client, work.id, apply=True)
    with ZipFile(new) as archive:
        assert archive.namelist()[0] == 'mimetype'
        assert archive.read('OPS/remote-images/i0001.png') == png
        assert b'../remote-images/i0001.png' in archive.read('OPS/text/c.xhtml')
        opf = archive.read('OPS/book.opf')
        assert b'ns0:' not in opf  # префиксы OPF не переименованы
        manifest = ET.fromstring(opf)
        assert any(item.get('media-type') == 'image/png' for item in manifest.iter())
    update_work(session, path, new)
    session.refresh(work)
    assert work.file_path == str(new) and work.sha1 == sha1_of_file(new)
    assert not path.exists()


def test_download_once_embeds_before_client_closes(png, monkeypatch):
    from backend.app import covers
    calls = []

    def serve(request):
        if request.url.host == 'cm.author.today':
            assert 'Cookie' not in request.headers
            calls.append(str(request.url))
            return httpx.Response(200, content=png)
        assert request.headers.get('Cookie') == 'session=secret'
        return httpx.Response(200, text='страница')

    # Такая cookie без домена раньше уходила вместе с клиентом книги на CDN.
    monkeypatch.setattr(authortoday.egress, 'at_client', lambda **kw:
                        httpx.Client(transport=httpx.MockTransport(serve), cookies=kw['cookies']))
    monkeypatch.setattr(authortoday, 'image_client', lambda *args:
                        httpx.Client(transport=httpx.MockTransport(serve), follow_redirects=False))
    monkeypatch.setattr(authortoday, '_parse_work_meta', lambda _: ('Книга', 'Автор', '', {}))
    monkeypatch.setattr(authortoday, '_is_free', lambda _: True)
    monkeypatch.setattr(authortoday, '_parse_chapters', lambda _: [{'id': 1}, {'id': 2}])
    monkeypatch.setattr(authortoday, '_fetch_chapter', lambda *args:
                        '<p><img src="https://cm.author.today/a.jpg"/></p>')
    monkeypatch.setattr(authortoday.time, 'sleep', lambda _: None)
    monkeypatch.setattr(covers, 'fetch_cover_bytes', lambda _: None)
    result = authortoday._download_once('https://author.today/work/4657', cookies={'session': 'secret'})
    assert calls == ['https://cm.author.today/a.jpg']
    with ZipFile(result.file_path) as archive:
        assert archive.read('EPUB/images/i0001.png') == png
        for n in (1, 2):
            assert b'src="images/i0001.png"' in archive.read(f'EPUB/chap_{n}.xhtml')


@pytest.mark.parametrize('ip', [
    '127.0.0.1', '10.1.2.3', '100.64.1.2', '100.127.255.254', '::1',
    '169.254.169.254', '224.0.0.1', '0.0.0.0', '240.0.0.1',
    'fc00::1', 'fe80::1', '::ffff:100.64.1.2',
])
def test_private_dns_rejected(ip, monkeypatch):
    monkeypatch.setattr(remote_images.socket, 'getaddrinfo', lambda *args, **kwargs:
                        [(socket.AF_INET6 if ':' in ip else socket.AF_INET,
                          socket.SOCK_STREAM, 6, '', (ip, 443))])
    calls = []
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as client,
        pytest.raises(ValueError, match='запрещённый IP'),
    ):
        fetch_image(client, 'https://host/a', 1024)
    assert not calls


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/a', 'http://10.0.0.1/a', 'http://100.64.0.1/a', 'http://[::1]/a',
])
def test_private_ip_literals(url, monkeypatch):
    from urllib.parse import urlsplit
    ip = urlsplit(url).hostname
    monkeypatch.setattr(remote_images.socket, 'getaddrinfo', lambda host, *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80))])
    with pytest.raises(ValueError, match='запрещённый IP'):
        remote_images.validate_image_url(url)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://host/a', 'https://host:8080/a',
                                 'http://host:0/a', 'https://user:secret@host/a'])
def test_invalid_scheme_port_or_credentials(url):
    with pytest.raises(ValueError, match='недопустимый URL'):
        remote_images.validate_image_url(url)


def test_mixed_public_private_dns_rejected(monkeypatch):
    monkeypatch.setattr(remote_images.socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))])
    with pytest.raises(ValueError, match='запрещённый IP'):
        remote_images.validate_image_url('https://host/a')


def test_redirect_to_private_is_rejected(monkeypatch):
    def dns(host, *args, **kwargs):
        ip = '10.0.0.1' if host == 'private' else '93.184.216.34'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]

    monkeypatch.setattr(remote_images.socket, 'getaddrinfo', dns)
    calls = []

    def serve(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'http://private/a'})

    # Даже клиент с follow_redirects=True не может обойти ручную проверку.
    with (
        httpx.Client(transport=httpx.MockTransport(serve), follow_redirects=True) as client,
        pytest.raises(ValueError, match='запрещённый IP'),
    ):
        fetch_image(client, 'https://host/a', 1024)
    assert calls == ['https://host/a']


def test_redirect_limit_and_relative_location(png):
    calls = []

    def serve(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'Location': '/next'})

    with (
        httpx.Client(transport=httpx.MockTransport(serve)) as client,
        pytest.raises(ValueError, match='больше 3'),
    ):
        fetch_image(client, 'https://host/a', 1024)
    assert len(calls) == 4
    assert calls[1] == 'https://host/next'


def test_cookie_jar_and_redirect_set_cookie_never_sent(png):
    calls = []

    def serve(request):
        assert 'Cookie' not in request.headers
        calls.append(request.url)
        if len(calls) == 1:
            return httpx.Response(302, headers={'Location': '/image', 'Set-Cookie': 'tracking=secret; Path=/'})
        return httpx.Response(200, content=png)

    with httpx.Client(transport=httpx.MockTransport(serve), cookies={'session': 'book-secret'}) as client:
        assert fetch_image(client, 'https://host/a', 1024) == png
    assert len(calls) == 2
    with image_client('reader-test') as client:
        assert not client.cookies
        assert not client.follow_redirects
        assert client.headers['User-Agent'] == 'reader-test'


def test_image_timeout_is_not_retried(monkeypatch):
    calls = []

    def serve(request):
        calls.append(request.url)
        raise httpx.ReadTimeout('timeout')

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        embed = ImageEmbedder(lambda url, limit: fetch_image(client, url, limit), '4657')
        markup = '<img src="https://host/a">'
        assert embed.rewrite(markup * 2) == markup * 2
    assert len(calls) == 1


def test_book_url_limit_includes_failures(png, caplog):
    calls = []

    def fetch(url, limit):
        calls.append(url)
        if url.endswith('/0'):
            raise ValueError('ошибка картинки')
        return png

    embed = ImageEmbedder(fetch, '4657')
    for n in range(301):
        markup = f'<img src="https://host/{n}">'
        result = embed.rewrite(markup)
        if n in (0, 300):
            assert result == markup
    assert len(calls) == 300
    assert len(embed.images) == 299
    assert 'images/i0001.png' in embed.rewrite('<img src="https://host/1">')
    assert '4657' in caplog.text and 'https://host/300' in caplog.text


def test_deadline_during_body(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(remote_images.time, 'monotonic', lambda: now[0])
    closed = []

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            for stamp in (10, 20, 31):
                now[0] = stamp
                yield b'x'

        def close(self):
            closed.append(True)

    with (
        httpx.Client(transport=httpx.MockTransport(lambda request:
                     httpx.Response(200, stream=SlowBody()))) as client,
        pytest.raises(ValueError, match='30 с'),
    ):
        fetch_image(client, 'https://host/a', 1024)
    assert closed


def test_deadline_shared_by_redirects(monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(remote_images.time, 'monotonic', lambda: now[0])

    def serve(request):
        calls.append(request.url)
        now[0] += 16
        return httpx.Response(302, headers={'Location': '/next'})

    with (
        httpx.Client(transport=httpx.MockTransport(serve)) as client,
        pytest.raises(ValueError, match='30 с'),
    ):
        fetch_image(client, 'https://host/a', 1024)
    assert len(calls) == 2


def test_deadline_after_dns(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(remote_images.time, 'monotonic', lambda: now[0])

    def slow_dns(*args, **kwargs):
        now[0] = 31
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]

    monkeypatch.setattr(remote_images.socket, 'getaddrinfo', slow_dns)
    calls = []
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as client,
        pytest.raises(ValueError, match='30 с'),
    ):
        fetch_image(client, 'https://host/a', 1024)
    assert not calls
