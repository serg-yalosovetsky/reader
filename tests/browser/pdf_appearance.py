"""Проверка обрезки и темы PDF в настоящем iframe.

CHROME_BIN=/path/to/chromium python tests/browser/pdf_appearance.py
"""
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DISABLE = '''
delete Uint8Array.prototype.toHex;
delete Uint8Array.prototype.toBase64;
delete Uint8Array.fromBase64;
delete Promise.withResolvers;
delete Promise.try;
delete Map.prototype.getOrInsertComputed;
delete WeakMap.prototype.getOrInsertComputed;
delete Set.prototype.intersection;
delete Array.prototype.findLast;
delete ArrayBuffer.prototype.transferToFixedLength;
delete globalThis.structuredClone;
delete URL.parse;
delete AbortSignal.any;
'''


def pdf():
    stream = b'0 0 0 rg 10 10 80 2 re f BT /F1 12 Tf 10 70 Td (Hello) Tj ET q 20 0 0 20 60 40 cm /Im1 Do Q'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> >>',
               b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 3 >>\nstream\n' + bytes([240, 60, 20]) + b'\nendstream']
    data = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(data)
    data += b'xref\n0 7\n0000000000 65535 f \n'
    data += b''.join(f'{n:010} 00000 n \n'.encode() for n in offsets[1:])
    return data + f'trailer\n<< /Root 1 0 R /Size 7 >>\nstartxref\n{start}\n%%EOF'.encode()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('CHROME_BIN'), args=['--no-sandbox'])
    page = browser.new_page()
    page.add_init_script(DISABLE)
    def serve(route):
        from urllib.parse import urlparse
        path = ROOT / urlparse(route.request.url).path.lstrip('/')
        if path.is_file():
            route.fulfill(path=str(path), content_type='text/javascript' if path.suffix in ('.js', '.mjs') else 'text/html')
        else:
            route.fulfill(status=404, body='')
    page.route('http://reader.test/**', serve)
    # Удаление API внутри worker выполняется ДО импорта исправленной сборки.
    page.route('**/pdf.worker.mjs?*', lambda route: route.fulfill(
        content_type='text/javascript', body=DISABLE + "\nawait import('./compat.mjs?v=20261016-1');\n" + re.sub(r"import './compat\.mjs[^']*'", '', (ROOT / 'frontend/vendor/foliate-js/vendor/pdfjs/pdf.worker.mjs').read_text())))
    page.route('http://reader.test/pdf-test', lambda route: route.fulfill(content_type='text/html', body='<!doctype html><html></html>'))
    page.goto('http://reader.test/pdf-test')
    result = page.evaluate('''async data => {
      const {makePDF} = await import('/frontend/vendor/foliate-js/pdf.js');
      await import('/frontend/vendor/foliate-js/fixed-layout.js');
      const book = await makePDF(new File([new Uint8Array(data)], 'test.pdf'));
      await book.setPDFOptions({pdfCrop: false, pdfBackground: 'original', theme: 'day'});
      const section = await book.sections[0].load();
      const original = section.getSize();
      await book.setPDFOptions({pdfCrop: true, pdfBackground: 'theme', theme: 'night', bg: '#15171a', fg: '#dddddd', dimImages: false});
      const cropped = section.getSize();
      const frame = document.createElement('iframe');
      document.body.append(frame);
      await new Promise(resolve => { frame.onload = resolve; frame.src = section.src; });
      await section.onZoom({doc: frame.contentDocument, scale: 2});
      const doc = frame.contentDocument;
      const pixels = () => doc.querySelector('canvas').getContext('2d');
      const pixel = (x,y) => [...pixels().getImageData(x*2,y*2,1,1).data].slice(0,3);
      const result = {original, cropped, background: pixel(50,20), image: pixel(70,50),
        ink: pixel(30,89),
        textInk: (() => {const d=pixels().getImageData(20,36,80,24).data; return Math.max(...Array.from(d).filter((_,i)=>i%4===0))})(),
        filter: doc.querySelector('#canvas').style.filter,
        offset: doc.querySelector('#layers').style.left,
        canvas: !!doc.querySelector('canvas'), text: !!doc.querySelector('.textLayer')};
      await book.setPDFOptions({pdfCrop: true, pdfBackground: 'theme', theme: 'night', bg: '#15171a', fg: '#dddddd', dimImages: true});
      await section.onZoom({doc, scale: 2});
      result.dimImage = pixel(70,50);
      await book.setPDFOptions({pdfCrop: true, pdfBackground: 'theme', theme: 'sepia', bg: '#f5ecd9'});
      await section.onZoom({doc, scale: 2});
      result.sepia = pixel(50,20);
      result.sepiaImage = pixel(70,50);
      await book.setPDFOptions({pdfCrop: false, pdfBackground: 'original', theme: 'day'});
      result.restored = section.getSize();
      book.destroy();
      return result;
    }''', list(pdf()))
    assert result['cropped']['width'] < result['original']['width'], result
    assert result['restored'] == result['original'], result
    assert result['filter'] == 'none' and result['offset'].startswith('-'), result
    assert result['canvas'] and result['text'], result
    assert result['background'] == [21, 23, 26], result
    assert result['image'] == [240, 60, 20], result
    assert min(result['ink']) > 200 and result['textInk'] > 200, result
    assert result['dimImage'] == [204, 51, 17], result
    assert result['sepia'] == [245, 236, 217], result
    assert result['sepiaImage'] == [231, 56, 17], result
    print('PDF crop/theme:', result)
    browser.close()
