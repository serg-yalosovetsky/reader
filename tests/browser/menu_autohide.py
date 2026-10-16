"""Автоскрытие: стабильная геометрия, листание, панели и клавиатура.

CHROME_BIN=/path/to/chromium python tests/browser/menu_autohide.py
"""
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True, executable_path=os.environ.get('CHROME_BIN'),
        args=['--no-sandbox'])
    page = browser.new_page(viewport={"width": 900, "height": 700})

    def serve(route):
        path = ROOT / urlparse(route.request.url).path.lstrip('/')
        if path.is_file():
            route.fulfill(path=str(path), content_type=(
                'text/javascript' if path.suffix == '.js' else 'text/css'))
        else:
            route.fulfill(content_type='text/html', body='''
              <link rel="stylesheet" href="/frontend/css/theme.css"><style>body{margin:0}</style>
              <div id="reader">
                <div id="reader-top" class="reader-bar reader-top"><button>Меню</button></div>
                <div id="view-host" class="view-host"></div>
                <div id="reader-bottom" class="reader-bar reader-bottom"><button>Вперёд</button></div>
                <aside class="panel" hidden>Оглавление</aside>
              </div>''')
    page.route('http://reader.test/**', serve)
    page.goto('http://reader.test/test')
    page.evaluate("""async () => {
      Object.defineProperty(document, 'fullscreenElement', {get: () => document.documentElement});
      window.menu = await import('/frontend/js/menu-autohide.js');
      menu.initMenuAutohide(); menu.setMenuFormat('pdf');
    }""")
    box = page.locator('#view-host').bounding_box()
    page.wait_for_timeout(5200)
    assert page.locator('#reader').evaluate("e => e.classList.contains('menu-hidden')")
    assert page.locator('#reader-top').evaluate('e => e.inert')
    page.keyboard.press('ArrowRight')
    assert page.locator('#reader').evaluate("e => e.classList.contains('menu-hidden')")
    assert page.locator('#view-host').bounding_box() == box
    page.locator('#view-host').click(position={"x": 450, "y": 350})
    assert not page.locator('#reader-top').evaluate('e => e.inert')
    page.evaluate("document.querySelector('.panel').hidden = false")
    page.wait_for_timeout(5200)
    assert not page.locator('#reader').evaluate("e => e.classList.contains('menu-hidden')")
    page.evaluate("document.querySelector('.panel').hidden = true")
    page.mouse.move(450, 350)
    page.wait_for_timeout(5200)
    assert page.locator('#reader').evaluate("e => e.classList.contains('menu-hidden')")
    page.keyboard.press('Tab')
    assert not page.locator('#reader-top').evaluate('e => e.inert')
    assert page.locator('#view-host').bounding_box() == box
    browser.close()
    print('ok: menu autohide browser')
