// Панель «Вид»: тема/размер/поля/шрифт/режим/колонки.
import { refreshMenuAutohide } from './menu-autohide.js'
import { $ } from './core/dom.js'
import { prefs, savePrefs, MARGIN_NAME, MARGIN_MAX } from './core/prefs.js'
import { currentWork } from './core/state.js'
import { currentFormat, applyViewStyles } from './reader-core.js'
import { pdfAsEpub, setPdfAsEpub } from './core/convert.js'

// Формат ответа отличает исходный PDF от его EPUB-версии.
const autohideKey = () => currentFormat.toLowerCase() === 'pdf' ? 'menuAutohidePdf' : 'menuAutohideOther'
document.addEventListener('reader-format-change', syncSettingsUI)

// ===================== Настройки вида (UI) =====================
export function syncSettingsUI() {
  $('#menu-autohide').checked = prefs[autohideKey()]
  $('#pdf-crop').closest('.setting-row').hidden = currentFormat.toLowerCase() !== 'pdf'
  $('#pdf-epub').closest('.setting-row').hidden = !(currentWork?.file_format === 'pdf' && currentFormat.toLowerCase() === 'epub')
  document.querySelectorAll('.swatch').forEach((b) => b.setAttribute('aria-current', String(b.dataset.theme === prefs.theme)))
  $('#font-val').textContent = Math.round(prefs.fontScale * 100) + '%'
  $('#margin-val').textContent = MARGIN_NAME[prefs.marginLevel]
  $('#font-family').value = prefs.fontFamily
  $('#flow-mode').value = prefs.flow
  $('#columns-mode').value = String(prefs.columns || 1)
  // «Колонки» актуальны только в режиме страниц.
  $('#columns-row').style.display = prefs.flow === 'paginated' ? '' : 'none'
  $('#pdf-crop').checked = prefs.pdfCrop
  $('#dim-images').checked = prefs.dimImages
  $('#pdf-epub').value = pdfAsEpub() ? '1' : '0'
}
document.querySelectorAll('.swatch').forEach((b) => b.addEventListener('click', () => {
  prefs.theme = b.dataset.theme
  document.documentElement.dataset.theme = prefs.theme
  savePrefs(); syncSettingsUI(); applyViewStyles()
}))
$('#font-inc').addEventListener('click', () => { prefs.fontScale = Math.min(2, prefs.fontScale + 0.1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#font-dec').addEventListener('click', () => { prefs.fontScale = Math.max(0.6, prefs.fontScale - 0.1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#margin-inc').addEventListener('click', () => { prefs.marginLevel = Math.min(MARGIN_MAX, prefs.marginLevel + 1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#margin-dec').addEventListener('click', () => { prefs.marginLevel = Math.max(0, prefs.marginLevel - 1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#font-family').addEventListener('change', (e) => { prefs.fontFamily = e.target.value; savePrefs(); applyViewStyles() })
$('#flow-mode').addEventListener('change', (e) => { prefs.flow = e.target.value; savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#columns-mode').addEventListener('change', (e) => { prefs.columns = parseInt(e.target.value, 10) || 1; savePrefs(); applyViewStyles() })
// PDF читать как EPUB (конвертация на сервере) или как есть — макетом страниц.
$('#pdf-epub')?.addEventListener('change', (e) => setPdfAsEpub(e.target.value === '1'))

for (const [id, key] of [['pdf-crop', 'pdfCrop'], ['dim-images', 'dimImages']]) {
  $('#' + id).addEventListener('change', e => {
    prefs[key] = e.target.checked
    savePrefs(); applyViewStyles()
  })
}

$('#menu-autohide').addEventListener('change', e => {
  prefs[autohideKey()] = e.target.checked
  savePrefs(); refreshMenuAutohide()
})
