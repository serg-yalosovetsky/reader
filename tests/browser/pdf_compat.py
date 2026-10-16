"""Реальный Chrome: рендер PDF с отключёнными новыми API в странице И воркере.

PYTHONPATH=/path/to/playwright python tests/browser/pdf_compat.py
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
    stream = b'0 0 0 rg 10 10 80 80 re f'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>',
               b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream']
    data = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(data)
    data += b'xref\n0 5\n0000000000 65535 f \n'
    data += b''.join(f'{n:010} 00000 n \n'.encode() for n in offsets[1:])
    return data + f'trailer\n<< /Root 1 0 R /Size 5 >>\nstartxref\n{start}\n%%EOF'.encode()


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
      await import('/frontend/vendor/foliate-js/pdf.js');
      if (!pdfjsLib.GlobalWorkerOptions.workerSrc.endsWith('?v=20261016-1'))
        throw new Error('URL воркера без версии');
      const task = pdfjsLib.getDocument({data: new Uint8Array(data)});
      const doc = await task.promise;
      const page = await doc.getPage(1);
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 100;
      const context = canvas.getContext('2d');
      await page.render({canvasContext: context, viewport: page.getViewport({scale: 1})}).promise;
      const center = [...context.getImageData(50, 50, 1, 1).data];
      const realWorker = Boolean(task._worker.port instanceof Worker);
      const pages = doc.numPages;
      await task.destroy();
      return {pages, center, realWorker};
    }''', list(pdf()))
    assert result == {'pages': 1, 'center': [0, 0, 0, 255], 'realWorker': True}, result
    print('PDF rendered in real worker without native APIs:', result)
    browser.close()
