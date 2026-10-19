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
               b'<< /Type /XObject /Subtype /Image /Width 3 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 9 >>\nstream\n' + bytes([255, 255, 255, 240, 60, 20, 0, 0, 0]) + b'\nendstream']
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
      localStorage.removeItem('reader.pdf-images.appearance-test');
      const {makePDF} = await import('/frontend/vendor/foliate-js/pdf.js');
      await import('/frontend/vendor/foliate-js/fixed-layout.js');
      const book = await makePDF(new File([new Uint8Array(data)], 'test.pdf'));
      await book.setPDFOptions({pdfCrop: false, theme: 'day'});
      const section = await book.sections[0].load();
      const original = section.getSize();
      // Границы теперь приходят с сервера; измерение canvas клиентом запрещено.
      await book.setPDFOptions({pdfCrop: true, cropData: {status: 'ready', book: [.08,.18,.92,.92], pages: [[.08,.18,.92,.92]]}, theme: 'night', bg: '#15171a', fg: '#dddddd', imageMode: 'theme', imageBookKey: 'appearance-test'});
      const cropped = section.getSize();
      const frame = document.createElement('iframe');
      document.body.append(frame);
      await new Promise(resolve => { frame.onload = resolve; frame.src = section.src; });
      await section.onZoom({doc: frame.contentDocument, scale: 2});
      const doc = frame.contentDocument;
      // Пиксели показанного PNG читаем через отдельный тестовый canvas.
      const pixels = () => {
        const img = doc.querySelector('#canvas > img');
        const canvas = document.createElement('canvas');
        canvas.width = img.width; canvas.height = img.height;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0);
        return ctx;
      };
      const pixel = (x,y) => {
        const base = [...pixels().getImageData(x*2,y*2,1,1).data].slice(0,3);
        const overlay = doc.querySelector('#image-original');
        if (!overlay) return base;
        const raw = overlay.getContext('2d').getImageData(x*2,y*2,1,1).data;
        const alpha = Number(overlay.style.opacity) * raw[3]/255;
        return base.map((v,i) => Math.round(v*(1-alpha)+raw[i]*alpha));
      };
      const result = {original, cropped, background: pixel(50,20), image: pixel(70,50),
        whiteImage: pixel(63,50), blackImage: pixel(77,50), ink: pixel(30,89),
        textInk: (() => {const d=pixels().getImageData(20,36,80,24).data; return Math.max(...Array.from(d).filter((_,i)=>i%4===0))})(),
        filter: doc.querySelector('#canvas').style.filter,
        offset: doc.querySelector('#layers').style.left,
        imageElement: !!doc.querySelector('#canvas > img'), text: !!doc.querySelector('.textLayer')};
      const firstImage = doc.querySelector('#canvas > img');
      const rect = firstImage.getBoundingClientRect();
      const wheel = (x,y,deltaX,deltaY=0,shiftKey=false) => {
        const event = new frame.contentWindow.WheelEvent('wheel', {clientX:rect.left+x*rect.width/firstImage.width*2,
          clientY:rect.top+y*rect.height/firstImage.height*2, deltaX, deltaY, shiftKey, bubbles:true, cancelable:true});
        doc.dispatchEvent(event);
        return event.defaultPrevented;
      };
      result.gesture = wheel(70,50,300);
      result.blend = Number(doc.querySelector('#image-original').style.opacity);
      result.blendedImage = pixel(70,50);
      result.sameImage = firstImage === doc.querySelector('#canvas > img');
      result.textGesture = wheel(30,30,100);
      result.verticalGesture = wheel(70,50,0,100);
      result.shiftGesture = wheel(70,50,0,-120,true);
      result.shiftBlend = Number(doc.querySelector('#image-original').style.opacity);
      result.savedBlend = Number(localStorage.getItem('reader.pdf-images.appearance-test'));
      const nextFrame = document.createElement('iframe'); document.body.append(nextFrame);
      await new Promise(resolve => {nextFrame.onload=resolve; nextFrame.src=section.src});
      await section.onZoom({doc:nextFrame.contentDocument,scale:2});
      result.nextBlend = Number(nextFrame.contentDocument.querySelector('#image-original').style.opacity);
      wheel(70,50,60);
      result.peerBlend = Number(nextFrame.contentDocument.querySelector('#image-original').style.opacity);
      wheel(70,50,-60);
      const reopened = await makePDF(new File([new Uint8Array(data)], 'test.pdf'));
      await reopened.setPDFOptions({theme:'night', bg:'#15171a', fg:'#dddddd', imageMode:'theme', imageBookKey:'appearance-test'});
      const restoredSection = await reopened.sections[0].load();
      const restoredFrame = document.createElement('iframe'); document.body.append(restoredFrame);
      await new Promise(resolve => {restoredFrame.onload=resolve; restoredFrame.src=restoredSection.src});
      await restoredSection.onZoom({doc:restoredFrame.contentDocument,scale:2});
      result.reopenedBlend = Number(restoredFrame.contentDocument.querySelector('#image-original').style.opacity);
      reopened.destroy();
      await book.setPDFOptions({pdfCrop: true, theme: 'night', bg: '#15171a', fg: '#dddddd', imageMode: 'dim', imageBookKey: 'appearance-test'});
      await section.onZoom({doc, scale: 2});
      result.dimImage = pixel(70,50);
      await book.setPDFOptions({theme:'terminal', bg:'#001100', fg:'#00cc66', imageMode:'theme'});
      await section.onZoom({doc,scale:2});
      result.terminalImage = pixel(70,50);
      await book.setPDFOptions({theme:'night', bg:'#15171a', fg:'#dddddd', imageMode:'original'});
      await section.onZoom({doc,scale:2});
      result.originalImage = pixel(70,50);
      await book.setPDFOptions({pdfCrop: true, theme: 'sepia', bg: '#f5ecd9'});
      await section.onZoom({doc, scale: 2});
      result.sepia = pixel(50,20);
      result.sepiaImage = pixel(70,50);
      await book.setPDFOptions({pdfCrop: false, theme: 'day'});
      await section.onZoom({doc, scale: 2});
      result.day = pixel(50,20);
      result.dayImage = pixel(70,50);
      result.dayInk = pixel(30,89);
      result.restored = section.getSize();
      book.destroy();
      return result;
    }''', list(pdf()))
    assert result['cropped']['width'] < result['original']['width'], result
    assert result['restored'] == result['original'], result
    assert result['filter'] == '' and result['offset'].startswith('-'), result
    assert result['imageElement'] and result['text'], result
    assert result['background'] == [21, 23, 26], result
    light = (240*.2126 + 60*.7152 + 20*.0722)/255
    themed = [round(bg*light + 221*(1-light)) for bg in [21,23,26]]
    assert result['image'] == themed, result
    assert result['whiteImage'] == result['background'], result
    assert result['blackImage'] == [221,221,221], result
    assert result['gesture'] and result['sameImage'] and result['shiftGesture'], result
    assert not result['textGesture'] and not result['verticalGesture'], result
    assert result['blend'] == .5 and result['shiftBlend'] == .3, result
    assert result['savedBlend'] == result['nextBlend'] == .3, result
    assert abs(result['reopenedBlend'] - .3) < 1e-9, result
    assert result['peerBlend'] == .4, result
    assert result['blendedImage'] == [round((v+raw)/2) for v,raw in zip(themed,[240,60,20])], result
    assert result['originalImage'] == [240,60,20], result
    assert result['terminalImage'][0] == 0 and 0 < result['terminalImage'][1] < 204, result
    assert min(result['ink']) > 200 and result['textInk'] > 200, result
    assert result['dimImage'] == [round(v*.35 + raw*.65) for v,raw in zip(themed,[240,60,20])], result
    assert result['sepia'] == [245, 236, 217], result
    assert result['sepiaImage'] == [231, 56, 17], result
    assert result['day'] == [255, 255, 255], result
    assert result['dayImage'] == [240, 60, 20], result
    assert result['dayInk'] == [0, 0, 0], result
    print('PDF crop/theme:', result)
    browser.close()
