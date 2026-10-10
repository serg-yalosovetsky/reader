"""Reproduce EPUB code backgrounds using the real reader styles in Chromium."""
import asyncio, threading
from resume_cache import QuietHandler, ThreadingHTTPServer
from playwright.async_api import async_playwright
async def main():
 s=ThreadingHTTPServer(('127.0.0.1',0), QuietHandler)
 threading.Thread(target=s.serve_forever,daemon=True).start()
 async with async_playwright() as p:
  b=await p.chromium.launch(args=['--no-sandbox'])
  page=await b.new_page()
  await page.route('**/api/**',lambda r:r.fulfill(json=[]))
  await page.goto(f'http://127.0.0.1:{s.server_port}/')
  result=await page.evaluate('''async () => {
   const {prefs}=await import('/js/core/prefs.js'); prefs.fontFamily='system';
   const {applyViewStyles}=await import('/js/reader-core.js');
   const results=[];
   for (const theme of ['day','sepia','grey','dusk','night','terminal','black','phosphor']) {
    document.documentElement.dataset.theme=theme; prefs.theme=theme;
    let css; applyViewStyles({renderer:{setAttribute(){},setStyles(s){css=s}}});
    const frame=document.createElement('iframe');document.body.append(frame);
    const doc=frame.contentDocument;
    doc.open();doc.write('<style>.highlight, pre {background:#fff;color:#111} .token {background:white;color:red}</style><div class="highlight"><pre id="block"><span class="token">await asyncio.sleep(10)</span></pre></div><p>Inline <code id="inline" style="background:white;color:black">asyncio</code></p>');doc.close();
    const style=doc.createElement('style');style.textContent=css;doc.head.append(style);
    const colors=el=>{const s=frame.contentWindow.getComputedStyle(el);return [s.color,s.backgroundColor]};
    const block=colors(doc.querySelector('.highlight')), token=colors(doc.querySelector('.token')), inline=colors(doc.querySelector('#inline'));
    const expected=document.createElement('div');expected.style.color='var(--fg)';expected.style.backgroundColor='var(--bg-soft)';document.body.append(expected);
    const main=getComputedStyle(expected);
    if(block[0]!==main.color||block[1]!==main.backgroundColor||inline[0]!==main.color||inline[1]!==main.backgroundColor||token[0]!==main.color||token[1]!=='rgba(0, 0, 0, 0)')throw Error(JSON.stringify({theme,block,token,inline,expected:[main.color,main.backgroundColor]}));
    results.push({theme,block,token});expected.remove();frame.remove();
   }return results;
  }''')
  print(result)
  await b.close()
 s.shutdown()
asyncio.run(main())
