"""Смена flow до первого показа PDF: без TypeError, позиция не теряется.

applyViewStyles ставит атрибут flow раньше, чем книга показана. Раньше геттер
index читал пустые развороты и падал «reading 'right'» на каждом открытии PDF.

CHROME_BIN=/path/to/chromium python tests/browser/fixed_layout_flow.py
"""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def pdf(count=4):
    stream = b'BT /F1 12 Tf 10 70 Td (Page) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Kids [' + ' '.join(f'{3+i} 0 R' for i in range(count)) + f'] /Count {count} >>').encode()]
    content, font = 3 + count, 4 + count
    objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 140] /Contents {content} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>'.encode()
                for _ in range(count)]
    objects += [b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream',
                b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
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
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def serve(route):
        path = ROOT / urlparse(route.request.url).path.lstrip('/')
        if path.is_file():
            route.fulfill(path=str(path), content_type='text/javascript' if path.suffix in ('.js', '.mjs') else 'text/html')
        else:
            route.fulfill(status=404, body='')
    page.route('http://reader.test/**', serve)
    # Воркер pdf.js — через compat.mjs, как в читалке (см. pdf_appearance.py).
    page.route('**/pdf.worker.mjs?*', lambda route: route.fulfill(
        content_type='text/javascript', body="await import('./compat.mjs?v=20261016-1');\n" + re.sub(r"import './compat\.mjs[^']*'", '', (ROOT / 'frontend/vendor/foliate-js/vendor/pdfjs/pdf.worker.mjs').read_text())))
    page.route('http://reader.test/flow-test', lambda route: route.fulfill(content_type='text/html', body='<!doctype html><html><body></body></html>'))
    page.goto('http://reader.test/flow-test')
    result = page.evaluate('''async data => {
      const {makePDF} = await import('/frontend/vendor/foliate-js/pdf.js');
      await import('/frontend/vendor/foliate-js/fixed-layout.js');
      const out = {};
      for (const [initial, next] of [['paginated', 'scrolled'], ['scrolled', 'paginated']]) {
        const book = await makePDF(new File([new Uint8Array(data)], 'flow.pdf'));
        const el = document.createElement('foliate-fxl');
        el.style.cssText = 'display:block;width:400px;height:560px';
        document.body.append(el);
        el.open(book);
        el.setAttribute('flow', initial);      // как applyViewStyles — до первого показа
        const before = el.index;
        await el.goTo(2);
        const shown = el.index;
        el.setAttribute('flow', next);         // смена уже после показа
        await new Promise(resolve => setTimeout(resolve, 1500));
        out[initial] = {before, shown, after: el.index, scrolled: el.scrolled};
        // Не удаляем: после remove() ResizeObserver зовёт onZoom с пустым doc — шум разборки.
      }
      return out;
    }''', list(pdf()))
    browser.close()

print(result, errors)
assert not [e for e in errors if 'RenderingCancelled' not in e], errors
for initial, r in result.items():
    assert r['before'] in (-1, 0), (initial, r)
    assert r['shown'] == 2, (initial, r)
    assert r['after'] == 2, (initial, r)
    assert r['scrolled'] == (initial == 'paginated'), (initial, r)
print('fixed-layout flow before show OK')
