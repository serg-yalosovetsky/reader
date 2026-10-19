"""Лента PDF: виртуализация, прогресс, переходы и смена режима."""
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def pdf(count=20):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Kids [' + ' '.join(f'{4+i*2} 0 R' for i in range(count)) + f'] /Count {count} >>').encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for i in range(count):
        stream = f'BT /F1 24 Tf 50 700 Td (Page {i+1}) Tj ET'.encode()
        objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Contents {5+i*2} 0 R /Resources << /Font << /F1 3 0 R >> >> >>'.encode(),
                    b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream']
    data, offsets = b'%PDF-1.4\n', [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{n:010} 00000 n \n'.encode() for n in offsets[1:])
    return data + f'trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\nstartxref\n{start}\n%%EOF'.encode()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('CHROME_BIN'), args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 412, 'height': 700},
                            device_scale_factor=2.625, is_mobile=True, has_touch=True)
    page.add_init_script('''
        window.revokedURLs = [];
        const revoke = URL.revokeObjectURL.bind(URL);
        URL.revokeObjectURL = url => { revokedURLs.push(url); revoke(url); };
    ''')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def serve(route):
        path = ROOT / urlparse(route.request.url).path.lstrip('/')
        if path.is_file():
            route.fulfill(path=str(path), content_type='text/javascript' if path.suffix in ('.js', '.mjs') else 'text/html')
        else:
            route.fulfill(content_type='text/html', body='<!doctype html><html><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body{margin:0;height:100%}foliate-view{display:block;width:100%;height:100%}</style></html>')

    page.route('http://reader.test/**', serve)
    page.goto('http://reader.test/pdf-test')
    page.evaluate('''async data => {
        await import('/frontend/vendor/foliate-js/view.js');
        const {makePDF} = await import('/frontend/vendor/foliate-js/pdf.js');
        window.book = await makePDF(new File([new Uint8Array(data)], 'scroll.pdf'));
        window.view = document.createElement('foliate-view');
        document.body.append(view);
        await view.open(book);
        window.locations = [];
        view.addEventListener('relocate', e => locations.push(e.detail.fraction));
        view.renderer.setAttribute('flow', 'scrolled');
        await view.goToFraction(0);
    }''', list(pdf()))
    page.wait_for_function('view.renderer.getContents().some(x => x.doc?.querySelector("#canvas > img"))')
    # Настоящий жест начинается внутри iframe и прокручивает внешний контейнер.
    cdp = page.context.new_cdp_session(page)
    before_touch = page.evaluate('view.renderer.scrollTop')
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 206, 'y': 560}]})
    for step in range(1, 21):
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': 206, 'y': 560 - step * 20}]})
        page.wait_for_timeout(16)
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    page.wait_for_function('start => view.renderer.scrollTop - start >= 320', arg=before_touch)
    page.wait_for_timeout(600)
    cdp.detach()
    # Уход за пределы окна виртуализации освобождает URL именно прежних страниц.
    old_images = page.evaluate("view.renderer.getContents().flatMap(x => [...x.doc.querySelectorAll('#canvas > img')].map(img => img.src))")
    assert old_images
    page.evaluate('view.renderer.scrollTop = view.renderer.scrollHeight / 2')
    page.wait_for_function('urls => urls.every(url => revokedURLs.includes(url))', arg=old_images)
    page.evaluate('view.goToFraction(0)')
    page.wait_for_function('view.renderer.scrollTop === 0')
    page.evaluate('locations.length = 0')
    for _ in range(6):
        page.evaluate('view.renderer.scrollBy(0, 450)')
        page.wait_for_timeout(150)
        assert page.evaluate('view.renderer.getContents().filter(x => x.doc?.querySelector("#canvas > img")).length') <= 5
    values = page.evaluate('locations')
    assert all(a <= b + 1e-9 for a, b in zip(values, values[1:])), values
    # Не ровно .5: это граница страниц 9/10, а при DPR 2.625 scrollTop округляется вниз.
    page.evaluate('view.goToFraction(.5004)')
    page.wait_for_function('view.lastLocation.section.current === 10')
    page.wait_for_timeout(600)
    result = page.evaluate('''() => ({index:view.renderer.index, fraction:view.lastLocation.fraction,
        count:view.renderer.getContents().filter(x => x.doc?.querySelector('#canvas > img')).length})''')
    assert result['index'] == 10 and abs(result['fraction'] - .5) < .001, result
    assert 1 <= result['count'] <= 5, result
    page.evaluate("view.renderer.setAttribute('flow', 'paginated')")
    page.wait_for_function('!view.renderer.scrolled && view.renderer.index === 10')
    page.wait_for_function('view.renderer.getContents().some(x => x.doc?.querySelector("#canvas > img"))')
    page.evaluate("view.renderer.setAttribute('flow', 'scrolled')")
    page.wait_for_function('view.renderer.scrolled && view.renderer.index === 10')
    page.evaluate('view.goToFraction(.525)')
    page.wait_for_function('Math.abs(view.lastLocation.fraction - .525) < .001')
    page.evaluate("view.renderer.setAttribute('flow', 'paginated')")
    page.wait_for_function('!view.renderer.scrolled && view.renderer.index === 10')
    page.evaluate("view.renderer.setAttribute('flow', 'scrolled')")
    page.wait_for_function('Math.abs(view.lastLocation.fraction - .525) < .001')
    # F5: новый View, тот же сохранённый прогресс из relocate.
    page.evaluate("""async () => {
        const {restorePosition} = await import('/frontend/js/core/position.js');
        const ratio = view.lastLocation.fraction;
        const locator = view.lastLocation.cfi;
        view.close(); view.remove();
        window.view = document.createElement('foliate-view');
        document.body.append(view);
        await view.open(book);
        view.renderer.setAttribute('flow', 'scrolled');
        await restorePosition(view, {ratio, locator, file_format:'pdf'}, '', 'pdf');
    }""")
    page.wait_for_function('Math.abs(view.lastLocation.fraction - .525) < .001')
    # Колесо и клавиши работают из iframe, где события не всплывают.
    before = page.evaluate('view.renderer.scrollTop')
    page.evaluate("""() => {
        const doc = view.renderer.getContents().find(x => x.index === 10).doc;
        doc.dispatchEvent(new WheelEvent('wheel', {deltaY:80, cancelable:true}));
        doc.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowDown', cancelable:true}));
    }""")
    assert abs(page.evaluate('view.renderer.scrollTop') - before - 128) < 2
    await_text = 'view.renderer.getContents().find(x => x.index === 10)?.doc.querySelector(".textLayer span")'
    page.wait_for_function(await_text)
    # Поисковый CFI из документа без canvas разрешается в настоящем textLayer.
    page.evaluate("""async () => {
        const doc = await book.sections[7].createDocument();
        const range = doc.createRange();
        range.selectNodeContents(doc.querySelector('.textLayer span'));
        await view.select(view.getCFI(7, range));
    }""")
    page.wait_for_function('view.lastLocation.section.current === 7')
    page.evaluate('view.goToFraction(1)')
    page.wait_for_function('view.renderer.atEnd && view.lastLocation.fraction === 1')
    assert not errors, errors
    print('PDF scroll:', result)
    browser.close()
