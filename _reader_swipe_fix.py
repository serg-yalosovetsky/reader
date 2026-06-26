"""
Fix two issues:
1. Remove duplicate scroll → chrome-hidden handler in attachKeysToDoc (causes jitter).
2. Patch paginator.js #onTouchEnd to skip snap() for horizontal swipes in paginated mode,
   so app.js gotoChapterStart handles chapter navigation instead.
Idempotent (checks if already patched).
"""
import difflib, pathlib, shutil
STAMP = ".bak17.20260617"
FR = pathlib.Path("/root/reader/frontend")

def patch(rel, repls):
    p = FR / rel
    src = orig = p.read_text(encoding="utf-8")
    for old, new in repls:
        if new in src and old not in src:
            print(f"[{rel}] already patched: {old[:60]!r}"); continue
        if old not in src:
            raise SystemExit(f"ANCHOR NOT FOUND in {rel}:\n{old[:120]!r}")
        src = src.replace(old, new, 1)
    if src != orig:
        shutil.copy2(p, str(p)+STAMP); p.write_text(src, encoding="utf-8")
        print(f"=== PATCHED {rel} ===")
        print("".join(difflib.unified_diff(orig.splitlines(True), src.splitlines(True), lineterm="", n=0))[:1200])
    else:
        print(f"[{rel}] no change")

# 1. Remove the duplicate doc.scroll → chrome-hidden handler inside attachKeysToDoc.
#    onBookScroll (on view.renderer scroll) is now the canonical handler with cooldown.
patch("js/app.js", [
    (
"""    // Авто-скрытие верх/низ панелей при прокрутке вниз (моб., «Лента»); вверх -> показать.
    let _lastScroll = -1
    e.detail.doc.addEventListener('scroll', () => {
      if (prefs.flow !== 'scrolled' || window.innerWidth > 560) return
      const y = (view?.renderer?.start) ?? (e.detail.doc.defaultView?.scrollY) ?? 0
      if (_lastScroll < 0) { _lastScroll = y; return }
      if (Math.abs(y - _lastScroll) < 12) return
      document.getElementById('reader')?.classList.toggle('chrome-hidden', y > _lastScroll && y > 40)
      _lastScroll = y
    }, { passive: true })""",
    ""  # remove entirely
    ),
])

# 2. Patch paginator.js #onTouchEnd to skip snap() for horizontal swipes in paginated mode.
#    In scrolled mode the early `if (this.scrolled) return` path is unchanged.
patch("vendor/foliate-js/paginator.js", [
    (
"""    #onTouchEnd() {
        this.#touchScrolled = false
        if (this.scrolled) return

        // XXX: Firefox seems to report scale as 1... sometimes...?
        // at this point I'm basically throwing `requestAnimationFrame` at
        // anything that doesn't work
        requestAnimationFrame(() => {
            if (globalThis.visualViewport.scale === 1)
                this.snap(this.#touchState.vx, this.#touchState.vy)
        })
    }""",
"""    #onTouchEnd() {
        const wasScrolled = this.#touchScrolled
        this.#touchScrolled = false
        if (this.scrolled) return

        // XXX: Firefox seems to report scale as 1... sometimes...?
        // at this point I'm basically throwing `requestAnimationFrame` at
        // anything that doesn't work
        requestAnimationFrame(() => {
            if (globalThis.visualViewport.scale === 1) {
                // Horizontal swipe in horizontal paginated mode: skip snap so app.js
                // gotoChapterStart handles chapter navigation instead of page-flip.
                if (!this.#vertical && wasScrolled &&
                    Math.abs(this.#touchState.vx ?? 0) >= Math.abs(this.#touchState.vy ?? 0) * 0.7)
                    return
                this.snap(this.#touchState.vx, this.#touchState.vy)
            }
        })
    }"""
    ),
])

# 3. Cache bust
patch("index.html", [
    ('<script type="module" src="/js/app.js?v=20260616j"></script>',
     '<script type="module" src="/js/app.js?v=20260617a"></script>'),
])

print("\nSWIPE FIX APPLIED.")
