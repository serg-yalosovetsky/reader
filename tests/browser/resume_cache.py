"""Real Chromium + real foliate integration; no production books/progress touched.
Run with Playwright installed: python tests/browser/resume_cache.py
"""
import asyncio
import io
import json
import threading
import os
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get('READER_TEST_ARTIFACTS', '/tmp/reader-resume-1761'))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
WORK = dict(id=1, title='Кеш страниц', author='Тест', file_format='epub',
            chapters_count=3, content_updated_at='2026-10-10T00:00:00')


def epub():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        z.writestr('book.opf', '''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">test-cache</dc:identifier><dc:title>Кеш страниц</dc:title><dc:language>ru</dc:language></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>''' + ''.join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(3)) + '''</manifest><spine>''' + ''.join(f'<itemref idref="c{i}"/>' for i in range(3)) + '</spine></package>')
        z.writestr('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Главы</title></head><body><nav epub:type="toc"><ol>' + ''.join(f'<li><a href="c{i}.xhtml">Глава {i}</a></li>' for i in range(3)) + '</ol></nav></body></html>')
        for i in range(3):
            z.writestr(f'c{i}.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Глава</title></head><body>' + ''.join(f'<p>Глава {i}, абзац {j}. ' + 'Сохраняем место чтения и готовим соседние страницы заранее. ' * 10 + '</p>' for j in range(70)) + '</body></html>')
    return buf.getvalue()


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'frontend'), **kwargs)
    def log_message(self, *args):
        pass


READ_WINDOW = '''() => new Promise((resolve, reject) => {
 const req = indexedDB.open('reader-resume-v1', 1);
 req.onsuccess = () => { const r = req.result.transaction('windows').objectStore('windows').get('1');
 r.onsuccess = () => resolve(r.result); r.onerror = reject }; req.onerror = reject;
})'''


async def run(flow='paginated', font='system'):
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    data = epub()
    progress = {'ratio': .48, 'locator': '', 'text_anchor': '', 'chapter': ''}
    delay = 0
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])
        context = await browser.new_context(viewport={'width': 412, 'height': 915}, service_workers='block')
        await context.add_init_script("localStorage.setItem('reader.prefs', JSON.stringify({v:3, theme:'day', fontFamily:" + json.dumps(font) + ", fontScale:1, marginLevel:2, columns:1, flow:" + json.dumps(flow) + '}))')
        async def api(route):
            nonlocal progress
            path = route.request.url.split('/api/', 1)[1]
            if path == 'reader/1/file':
                await asyncio.sleep(delay)
                try:
                    await route.fulfill(body=data, content_type='application/epub+zip', headers={'X-Book-Format':'epub'})
                except Exception:
                    pass  # the cancellation test deliberately closes this page
                return
            if path == 'progress/1' and route.request.method == 'PUT':
                progress = route.request.post_data_json
            payload = {}
            if path == 'library/1': payload = WORK
            elif path == 'library': payload = [WORK]
            elif path == 'progress/1': payload = progress
            elif path == 'progress': payload = {'1': progress}
            elif path.endswith('/history') or path in ('monitored','calibre/books','bookmarks/1','highlights/1'): payload = []
            elif path == 'ingest/jobs': payload = {'active':0, 'jobs':[]}
            elif path == 'reader/1/toc': payload = {'items':[]}
            await route.fulfill(json=payload)
        await context.route('**/api/**', api)
        if font == 'pt-sans':
            # Exercise CSS/WOFF lifetime without relying on Google's availability.
            font_data = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf').read_bytes()
            await context.route('https://fonts.googleapis.com/**', lambda r: r.fulfill(
                body="@font-face {font-family:'PT Sans';src:url('https://fonts.gstatic.com/test.ttf')}",
                content_type='text/css', headers={'Access-Control-Allow-Origin':'*'}))
            await context.route('https://fonts.gstatic.com/**', lambda r: r.fulfill(
                body=font_data, content_type='font/ttf', headers={'Access-Control-Allow-Origin':'*'}))
        page = await context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: print('CONSOLE',m.text) if 'resume cache' in m.text else None)
        await page.goto(url + '/?open=1')
        await page.evaluate("async () => { window.readerState=await import('/js/core/state.js'); window.resumeCache=await import('/js/core/resume-cache.js') }")
        await page.wait_for_function("() => window.readerState.view?.lastLocation?.cfi")
        await page.wait_for_timeout(3000)
        # Move close enough to a chapter boundary that the cache crosses chapters.
        await page.evaluate("async () => { const s = await import('/js/core/state.js'); await s.view.goTo('c1.xhtml'); await s.view.next(); await s.view.next() }")
        initial = await page.evaluate("async () => (await import('/vendor/foliate-js/epubcfi.js')).collapse((await import('/js/core/state.js')).view.lastLocation.cfi)")
        window = None
        for _ in range(100):
            await page.wait_for_timeout(150)
            window = await page.evaluate(READ_WINDOW)
            if window and len(window['pages']) == 21:
                center_start = await page.evaluate("async cfi => (await import('/vendor/foliate-js/epubcfi.js')).collapse(cfi)", window['pages'][window['center']]['position']['locator'])
                if center_start == initial: break
        if not window: print('DEBUG', await page.evaluate("async () => { const s=await import('/js/core/state.js'); return {snapshot:typeof s.view.renderer.snapshot, snap:s.view.renderer.snapshot(), warmer:!!document.querySelector('.resume-warmer'), loc:s.view.lastLocation?.cfi} }"), errors)
        assert window and len(window['pages']) == 21, ('window not warmed',window and len(window['pages']))
        assert center_start == initial, ('cache center drift',center_start,initial)
        assert window['center'] == 10
        assert len(window['documents']) >= 2, 'window should cross chapter boundary'
        assert all('blob:' not in d for d in window['documents'].values())
        if font == 'pt-sans': assert all('data:font/ttf' in d for d in window['documents'].values())
        assert await page.evaluate("async () => (await import('/vendor/foliate-js/epubcfi.js')).collapse((await import('/js/core/state.js')).view.lastLocation.cfi)") == initial, 'warmer moved live reader'
        await page.wait_for_timeout(1000)
        delay = 5
        await page.evaluate('''async work => {
          const m=await import('/js/reader-core.js'); window.resumeClickStart=performance.now();
          void m.openReader(work);
        }''', WORK)
        await page.wait_for_selector('#view-host[data-resume-visible=true]',timeout=1500)
        elapsed = await page.evaluate('(performance.now() - window.resumeClickStart) / 1000')
        assert elapsed < 1.5, elapsed
        # Compare cached iframe text/geometry with its live counterpart after handoff.
        cached_rect = await page.eval_on_selector('.resume-clip iframe', 'el => { const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height} }')
        await page.click('#next-btn')
        await page.click('#next-btn')
        target = await page.evaluate("async () => (await import('/js/core/resume-cache.js')).resumePosition().locator")
        target_start = await page.evaluate("async target => (await import('/vendor/foliate-js/epubcfi.js')).collapse(target)",target)
        if target_start == initial: print('TURN DEBUG', {'initial':initial,'target':target,'offsets':[p['offset'] for p in window['pages']]},errors)
        assert target_start != initial
        await page.screenshot(path=str(ARTIFACTS / f'resume-{flow}-target-cached.png'))
        await page.evaluate("async () => { window.readerState=await import('/js/core/state.js'); window.resumeCache=await import('/js/core/resume-cache.js') }")
        await page.wait_for_function("() => !window.resumeCache.resumePosition() && window.readerState.view?.lastLocation?.cfi", timeout=15000)
        actual = await page.evaluate("async () => (await import('/vendor/foliate-js/epubcfi.js')).collapse((await import('/js/core/state.js')).view.lastLocation.cfi)")
        if actual != target_start:
            print('HANDOFF DEBUG',await page.evaluate("async () => { const s=await import('/js/core/state.js'); const r=s.view.renderer; const sn=r.snapshot(); return {clip:sn.clip, frame:sn.frame, host: [document.querySelector('#view-host').clientWidth,document.querySelector('#view-host').clientHeight],loc:s.view.lastLocation,font:sn.doc.defaultView.getComputedStyle(sn.doc.body).font} }"), 'cachekey',window['key'], 'cachedpage', window['pages'][12])
            await page.screenshot(path=str(ARTIFACTS / f'resume-{flow}-target-live.png'))
        assert actual == target_start, ('handoff lost cached turns', actual,target)
        await page.screenshot(path=str(ARTIFACTS / f'resume-{flow}-live.png'))
        # Close during slow open: late completion must not reopen the book.
        await page.wait_for_timeout(5500)
        await page.reload(wait_until='domcontentloaded')
        await page.wait_for_selector('#view-host[data-resume-visible=true]',timeout=3000)
        await page.screenshot(path=str(ARTIFACTS / f'resume-{flow}-cached.png'))
        await page.click('#back-btn')
        await page.wait_for_timeout(5500)
        assert await page.locator('#reader').is_hidden()
        assert await page.evaluate("async () => (await import('/js/core/state.js')).view") is None
        # A changed book and changed preferences must reject this window.
        accepted = await page.evaluate("""async () => {
          const m=await import('/js/core/resume-cache.js'); const p=await import('/js/core/prefs.js');
          const host=document.querySelector('#view-host'); document.querySelector('#reader').hidden=false;
          const work=""" + json.dumps(WORK) + """;
          const changed = await m.showResume({...work, content_updated_at:'new'},p.prefs,host);
          const style = await m.showResume(work,{...p.prefs,fontScale:1.5},host);
          m.stopResume(); return [changed,style]; }""")
        assert accepted == [False,False]
        assert not errors, errors
        print(json.dumps({'flow':flow,'font':font,'cached_first_page_seconds':round(elapsed,3),
                          'pages':len(window['pages']),'documents':len(window['documents']),
                          'cache_bytes':window['bytes'],'cached_rect':cached_rect,
                          'handoff':True,'close_during_load':True,'invalidation':True}))
        await browser.close()
    server.shutdown()


if __name__ == '__main__':
    asyncio.run(run())
    asyncio.run(run('scrolled'))
    asyncio.run(run('paginated', 'pt-sans'))
