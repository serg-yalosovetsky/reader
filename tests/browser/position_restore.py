"""Позиция чтения в живом Chromium + foliate (serg/tasks#1853). Прод не трогается.
Run with Playwright installed: python tests/browser/position_restore.py

1. Неуникальный якорь: в конце каждой главы одинаковая авторская плашка, доля
   испорчена (кламп/масштаб). Раньше книга открывалась на первой главе с этой
   плашкой от «доля × секции»; теперь — в главе из CFI.
2. Тот же файл (book_rev совпал) — восстановление ровно по CFI.
3. «Прочитано» (at_end) — только на последней странице, не в середине последней главы.
4. Уход вкладки (pagehide) пишет место сразу, не дожидаясь дебаунса.
5. Позиция, не ушедшая на сервер, досылается и открывается при следующем входе.
"""
import asyncio
import hashlib
import io
import json
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
CHAPTERS = 4
WORK = dict(id=1, title='Плашки', author='Тест', file_format='epub',
            chapters_count=CHAPTERS, content_updated_at='2026-10-14T00:00:00')
PLAQUE = ('Анонсы новых глав, версии с иллюстрациями, а также другие произведения '
          'нашей команды ищите в нашем телеграм-канале. ') * 12


def epub():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        z.writestr('book.opf', '''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">test-plaque</dc:identifier><dc:title>Плашки</dc:title><dc:language>ru</dc:language></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>''' + ''.join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(CHAPTERS)) + '''</manifest><spine>''' + ''.join(f'<itemref idref="c{i}"/>' for i in range(CHAPTERS)) + '</spine></package>')
        z.writestr('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Главы</title></head><body><nav epub:type="toc"><ol>' + ''.join(f'<li><a href="c{i}.xhtml">Глава {i}</a></li>' for i in range(CHAPTERS)) + '</ol></nav></body></html>')
        for i in range(CHAPTERS):
            # Главы одинаковой длины → последняя страница каждой начинается в
            # одном и том же месте плашки → якорь одинаковый во всех главах.
            body = ''.join(f'<p>Глава {i}, абзац {j:02d}. ' + 'Текст главы для нескольких страниц чтения. ' * 8 + '</p>' for j in range(20))
            body += ''.join(f'<p>{PLAQUE}</p>' for _ in range(3))
            z.writestr(f'c{i}.xhtml', f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Глава {i}</title></head><body>{body}</body></html>')
    return buf.getvalue()


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'frontend'), **kwargs)

    def log_message(self, *args):
        pass


SECTION = "async () => (await import('/js/core/state.js')).view.lastLocation?.section?.current"
LIVE_POS = """async () => {
  const s = await import('/js/core/state.js'); const p = await import('/js/core/position.js')
  const l = s.view.lastLocation
  return { locator: l.cfi, text_anchor: p.captureAnchor(l.range), ratio: l.fraction,
           chapter: l.tocItem?.label || '', section: l.section.current, atEnd: s.view.renderer.atEnd }
}"""
TO_LAST_PAGE_OF = """async i => {
  const s = await import('/js/core/state.js'); const v = s.view
  await v.goTo(i + 1 === %d ? 'c' + i + '.xhtml' : 'c' + (i + 1) + '.xhtml')
  if (i + 1 < %d) { await v.prev() } else { for (let k = 0; k < 200 && !v.renderer.atEnd; k++) await v.next() }
  await new Promise(r => setTimeout(r, 300))
}""" % (CHAPTERS, CHAPTERS)
CLEAN = """async () => {
  localStorage.removeItem('reader.pendingProgress.v1')
  await new Promise(r => { const q = indexedDB.open('reader-resume-v1', 1); q.onsuccess = () => { const t = q.result.transaction('windows', 'readwrite'); t.objectStore('windows').clear(); t.oncomplete = t.onerror = () => { q.result.close(); r() } }; q.onerror = r })
}"""
REOPEN = """async work => {
  const m = await import('/js/reader-core.js')
  await m.openReader(work)
  await new Promise(r => setTimeout(r, 800))
}"""


async def reopen(page, state, clean=True):
    state['freeze'] = True
    if clean:
        await page.evaluate(CLEAN)
    await page.evaluate(REOPEN, WORK)
    state['freeze'] = False


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    data = epub()
    rev = hashlib.sha1(data).hexdigest()
    # freeze: на время переоткрытия PUT'ы (запись места закрываемой книги) не
    # меняют подготовленную тестом серверную позицию.
    state = {'progress': {'ratio': 0, 'locator': '', 'text_anchor': ''}, 'puts': [], 'fail_put': False, 'freeze': False}
    errors = []
    report = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox'])
        ctx = await browser.new_context(viewport={'width': 412, 'height': 915}, service_workers='block')
        await ctx.add_init_script("localStorage.setItem('reader.prefs', JSON.stringify({v:3, theme:'day', fontFamily:'system', fontScale:1, marginLevel:2, columns:1, flow:'paginated'}))")

        async def api(route):
            req = route.request
            path = req.url.split('/api/', 1)[1]
            if path == 'reader/1/file':
                await route.fulfill(body=data, content_type='application/epub+zip', headers={'X-Book-Format': 'epub'})
                return
            if path == 'progress/1' and req.method == 'PUT':
                body = req.post_data_json
                # fail_put — «нет сети» для обычных записей; досылка (replay)
                # после перезапуска уже идёт по живой сети.
                if state['fail_put'] and not body.get('replay'):
                    await route.abort()
                    return
                state['puts'].append(body)
                if not state['freeze'] and not (body.get('replay') and body.get('saved_at', 0) <= state['progress'].get('saved_at', 0)):
                    state['progress'] = body
                await route.fulfill(json=state['progress'])
                return
            payload = {}
            if path == 'library/1': payload = WORK
            elif path == 'library': payload = [WORK]
            elif path == 'progress/1': payload = state['progress']
            elif path == 'progress': payload = {'1': state['progress'].get('ratio', 0)}
            elif path == 'progress/finished': payload = []
            elif path.endswith('/history') or path in ('monitored', 'calibre/books', 'bookmarks/1', 'highlights/1'): payload = []
            elif path == 'ingest/jobs': payload = {'active': 0, 'jobs': []}
            elif path == 'reader/1/toc': payload = {'items': []}
            await route.fulfill(json=payload)

        await ctx.route('**/api/**', api)
        page = await ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(url + '/?open=1')
        await page.wait_for_function("async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi")
        await page.wait_for_timeout(1500)

        print('STEP 1', flush=True)
        # --- 1. неуникальный якорь + испорченная доля ---
        target = 2
        await page.evaluate(TO_LAST_PAGE_OF, target)
        pos = await page.evaluate(LIVE_POS)
        assert pos['section'] == target, pos
        others = []
        for i in (0, 1):
            await page.evaluate(TO_LAST_PAGE_OF, i)
            others.append((await page.evaluate(LIVE_POS))['text_anchor'])
        assert all(a[:60] == pos['text_anchor'][:60] for a in others), ('якорь должен повторяться', others, pos['text_anchor'])
        state['progress'] = {'ratio': 0.2, 'locator': pos['locator'], 'text_anchor': pos['text_anchor'],
                             'chapter': pos['chapter'], 'book_rev': '', 'saved_at': 1}
        await reopen(page, state)
        got = await page.evaluate(SECTION)
        assert got == target, ('книга открылась не в той главе (якорь-плашка)', got, target)
        report['duplicate_anchor_restores_section'] = got

        print('STEP 2', flush=True)
        # --- 2. тот же файл — точно по CFI ---
        state['progress'] = {**state['progress'], 'book_rev': rev, 'text_anchor': 'якоря нет в книге вовсе ' * 3}
        await reopen(page, state)
        cfi_now = await page.evaluate("async () => (await import('/vendor/foliate-js/epubcfi.js')).collapse((await import('/js/core/state.js')).view.lastLocation.cfi)")
        cfi_want = await page.evaluate("async c => (await import('/vendor/foliate-js/epubcfi.js')).collapse(c)", pos['locator'])
        assert cfi_now == cfi_want, ('при совпавшем book_rev — ровно CFI', cfi_now, cfi_want)
        report['same_rev_uses_cfi'] = True

        print('STEP 3', flush=True)
        # --- 3. at_end только на последней странице ---
        state['puts'].clear()
        await page.evaluate(f"async () => {{ const v=(await import('/js/core/state.js')).view; await v.goTo('c{CHAPTERS - 1}.xhtml'); await v.next(); await new Promise(r=>setTimeout(r,1500)) }}")
        mid = state['puts'][-1]
        assert mid['at_end'] is False, ('середина последней главы — не прочитано', mid)
        await page.evaluate(TO_LAST_PAGE_OF, CHAPTERS - 1)
        await page.wait_for_timeout(500)
        assert state['puts'][-1]['at_end'] is True, state['puts'][-1]
        report['at_end_only_on_last_page'] = True

        print('STEP 4', flush=True)
        # --- 4. pagehide пишет сразу ---
        await page.evaluate("async () => { const v=(await import('/js/core/state.js')).view; await v.goTo('c1.xhtml'); await v.next() }")
        state['puts'].clear()
        await page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        await page.wait_for_timeout(300)  # меньше дебаунса 900 мс
        assert state['puts'], 'pagehide не записал позицию'
        flushed = await page.evaluate(SECTION)
        assert await page.evaluate("async c => (await import('/js/core/state.js')).view.resolveCFI(c).index", state['puts'][-1]['locator']) == flushed
        report['pagehide_flush'] = True

        print('STEP 5', flush=True)
        # --- 5. офлайн-позиция досылается и открывается ---
        state['fail_put'] = True
        await page.evaluate("async () => { const v=(await import('/js/core/state.js')).view; await v.goTo('c3.xhtml'); await v.next(); await new Promise(r=>setTimeout(r,1500)) }")
        pending = await page.evaluate("() => JSON.parse(localStorage.getItem('reader.pendingProgress.v1') || '{}')['1']")
        assert pending and pending['body']['locator'], ('без сети позиция должна остаться в очереди', pending)
        offline_section = await page.evaluate(SECTION)
        state['progress'] = {**state['progress'], 'saved_at': 1, 'locator': pos['locator'], 'book_rev': rev}
        state['puts'].clear()
        # Вкладку убили (PWA выгрузили) без сети — keepalive тоже не дошёл.
        await page.reload()
        await page.wait_for_function("async () => (await import('/js/core/state.js')).view?.lastLocation?.cfi")
        await page.wait_for_timeout(1500)
        state['fail_put'] = False
        assert await page.evaluate(SECTION) == offline_section, ('открылась не неотправленная позиция', await page.evaluate(SECTION), offline_section)
        assert any(b.get('replay') for b in state['puts']), 'неотправленная позиция не дослана'
        assert state['progress']['locator'] == pending['body']['locator'], 'сервер не получил досланную позицию'
        report['offline_replay'] = True

        assert not errors, errors
        print(json.dumps(report, ensure_ascii=False))
        await browser.close()
    server.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
