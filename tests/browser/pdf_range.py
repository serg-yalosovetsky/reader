"""Частичная загрузка нелинеаризованного PDF и постоянный кэш блоков."""
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def pdf(count=60):
    # Как у реальных PDF: каталог, дерево страниц и потоки содержимого — компактно
    # в начале, тяжёлые картинки — после. Если перемешать описания страниц с
    # картинками, pdf.js обходит дерево страниц через весь файл.
    image_base = 3 + count * 2
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Kids [' + ' '.join(f'{3+i*2} 0 R' for i in range(count)) + f'] /Count {count} >>').encode()]
    stream = b'q 500 0 0 700 50 50 cm /Im0 Do Q'
    for i in range(count):
        objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Contents {4+i*2} 0 R /Resources << /XObject << /Im0 {image_base+i} 0 R >> >> >>'.encode(),
                    b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream']
    for i in range(count):
        image = os.urandom(256 * 256 * 3)
        objects.append(b'<< /Type /XObject /Subtype /Image /Width 256 /Height 256 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length %d >>\nstream\n' % len(image) + image + b'\nendstream')
    data, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{n:010} 00000 n \n'.encode() for n in offsets[1:])
    return bytes(data) + f'trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\nstartxref\n{start}\n%%EOF'.encode()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('CHROME_BIN'), args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 600, 'height': 700})
    data = pdf()
    requests = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def serve(route):
        request = route.request
        path = urlparse(request.url).path
        if path == '/api/reader/1/file':
            headers = {'Content-Type': 'application/pdf', 'ETag': '"range-test"', 'X-Book-SHA1': 'test-rev',
                       'Content-Length': str(len(data)), 'Accept-Ranges': 'bytes'}
            if request.method == 'HEAD':
                route.fulfill(status=200, headers=headers, body=b'')
                return
            value = request.headers.get('range')
            assert value, 'Полное скачивание PDF запрещено'
            begin, end = map(int, value.removeprefix('bytes=').split('-'))
            end = min(end, len(data)-1)
            body = data[begin:end+1]
            requests.append((begin, end))
            headers.update({'Content-Range': f'bytes {begin}-{end}/{len(data)}', 'Content-Length': str(len(body))})
            route.fulfill(status=206, headers=headers, body=body)
            return
        file = ROOT / path.lstrip('/')
        if file.is_file():
            route.fulfill(path=str(file), content_type='text/javascript' if file.suffix in ('.js', '.mjs') else 'text/plain')
        else:
            route.fulfill(content_type='text/html', body='<!doctype html><style>html,body{margin:0;height:100%}foliate-view{display:block;height:100%}</style>')

    page.route('http://localhost/**', serve)
    page.goto('http://localhost/pdf-range')
    page.evaluate('''async () => {
        await caches.delete('reader-pdf-chunks-v1');
        await import('/frontend/vendor/foliate-js/view.js');
        window.openRange = async () => {
            const {makePDF} = await import('/frontend/vendor/foliate-js/pdf.js');
            window.book = await makePDF({url:'/api/reader/1/file?original=1'});
            window.view = document.createElement('foliate-view');
            document.body.append(view);
            await view.open(book);
            await view.goToFraction(29 / 60);
        };
        await openRange();
    }''')
    page.wait_for_function('view.renderer.getContents().some(x => x.index === 29 && x.doc?.querySelector("canvas"))')
    page.wait_for_timeout(4000)
    downloaded = sum(end-begin+1 for begin, end in requests)
    assert downloaded < len(data) * .65, (downloaded, len(data), requests)
    before = len(requests)
    page.evaluate('view.goToFraction(30 / 60)')
    page.wait_for_function('view.renderer.getContents().some(x => x.index === 30 && x.doc?.querySelector("canvas"))')
    # Следующая волна предзагрузки вправе получить один новый блок.
    page.wait_for_timeout(1000)
    assert len(requests) - before <= 2, requests[before:]
    known = set(requests)
    before = len(requests)
    page.evaluate('''async () => { view.close(); view.remove(); await openRange(); }''')
    page.wait_for_function('view.renderer.getContents().some(x => x.index === 29 && x.doc?.querySelector("canvas"))')
    page.wait_for_timeout(2000)
    assert not (set(requests[before:]) & known), requests[before:]
    assert not errors, errors
    browser.close()
    print(f'PDF range OK: {downloaded}/{len(data)} bytes')
