"""Внешний сервер молчит 60 с, глава открывается за <5 с, включая resume-cache.

CHROME_BIN=/path/to/chromium python tests/browser/remote_images.py
"""
import asyncio
import io
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from zipfile import ZipFile

from playwright.async_api import async_playwright
from position_restore import WORK, QuietHandler, epub


class Handler(QuietHandler):
    def do_GET(self):
        if self.path == '/hang.jpg':
            # Соединение принято, но даже заголовки не приходят до таймаута.
            time.sleep(60)
            self.close_connection = True
            return
        super().do_GET()


def illustrated_epub(url):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(epub())) as source, ZipFile(output, 'w') as dest:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == 'c0.xhtml':
                data = data.replace(b'<body>', ('<body><p id="remote-marker">Глава уже показана</p>'
                                    f'<img width="100" height="80" src="{url}/hang.jpg"/>').encode())
            dest.writestr(info, data)
    return output.getvalue()


async def until(page, js, timeout, what):
    """wait_for_function не ждёт Promise из предиката (он истинен сразу) — опрашиваем сами."""
    deadline = time.monotonic() + timeout
    while not await asyncio.wait_for(page.evaluate(js), 5):
        assert time.monotonic() < deadline, f'{what}: не дождались за {timeout} с'
        await asyncio.sleep(0.1)


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    data = illustrated_epub(url)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=os.environ.get('CHROME_BIN'), args=['--no-sandbox'])
            ctx = await browser.new_context(viewport={'width': 412, 'height': 915}, service_workers='block')
            await ctx.add_init_script("localStorage.setItem('reader.prefs', JSON.stringify({v:3,theme:'day',fontFamily:'system',fontScale:1,marginLevel:2,columns:1,flow:'paginated'}))")

            async def api(route):
                path = route.request.url.split('/api/', 1)[1]
                if path == 'reader/1/file':
                    await route.fulfill(body=data, content_type='application/epub+zip', headers={'X-Book-Format': 'epub'})
                    return
                payload = {}
                if path == 'library/1':
                    payload = WORK
                elif path == 'library':
                    payload = [WORK]
                elif path == 'progress/1':
                    payload = {'ratio': 0, 'locator': '', 'text_anchor': ''}
                elif path == 'progress':
                    payload = {'1': 0}
                elif path.endswith('/history') or path in ('progress/finished', 'monitored', 'calibre/books', 'bookmarks/1', 'highlights/1'):
                    payload = []
                elif path == 'ingest/jobs':
                    payload = {'active': 0, 'jobs': []}
                elif path == 'reader/1/toc':
                    payload = {'items': []}
                await route.fulfill(json=payload)

            await ctx.route('**/api/**', api)
            page = await ctx.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            start = time.monotonic()
            await page.goto(url + '/?open=1', wait_until='domcontentloaded')
            await until(page, "async () => { const v=(await import('/js/core/state.js')).view; return !!v?.lastLocation?.cfi && !!v.renderer?.getContents?.()[0]?.doc?.querySelector('#remote-marker') }", 10, 'глава')
            cold = time.monotonic() - start
            assert cold < 5, cold
            assert await page.evaluate("async () => { const d=(await import('/js/core/state.js')).view.renderer.getContents()[0].doc; const i=d.querySelector('img'); return i.loading === 'lazy' && i.getAttribute('src').endsWith('/hang.jpg') && i.width === 100 }")
            # Ждём настоящего снимка, затем имитируем старый кеш с блокирующим src.
            await until(page, """() => new Promise(resolve => {
              const q=indexedDB.open('reader-resume-v1',1); q.onsuccess=()=>{
                const r=q.result.transaction('windows').objectStore('windows').get('1');
                r.onsuccess=()=>{ q.result.close(); resolve(!!r.result?.pages?.length) }
              }; q.onerror=()=>resolve(false)
            })""", 15, 'снимок resume-cache')
            await page.evaluate("""url => new Promise(resolve => {
              const q=indexedDB.open('reader-resume-v1',1); q.onsuccess=()=>{
                const tx=q.result.transaction('windows','readwrite'), s=tx.objectStore('windows'), r=s.get('1');
                r.onsuccess=()=>{ const record=r.result; for(const key of Object.keys(record.documents))
                  record.documents[key]=record.documents[key].replace(/<body([^>]*)>/i, '<body$1><img width="100" height="80" src="'+url+'/hang.jpg">');
                  s.put(record) }; tx.oncomplete=()=>{q.result.close();resolve()}
              }
            })""", url)
            start = time.monotonic()
            await page.reload(wait_until='domcontentloaded')
            await page.wait_for_function("document.querySelector('[data-resume-visible=true]')", timeout=4500)
            assert await page.evaluate("""() => {
              const frame=document.querySelector('.resume-page iframe');
              return frame && ![...frame.contentDocument.querySelectorAll('img')].some(i => /^https?:/.test(i.getAttribute('src') || ''))
            }""")
            warm = time.monotonic() - start
            assert warm < 5, warm
            assert not errors, errors
            print(json.dumps({'cold_seconds': cold, 'warm_seconds': warm}))
            await browser.close()
    finally:
        server.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
