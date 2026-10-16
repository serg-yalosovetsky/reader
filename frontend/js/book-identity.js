// Ручное название и автор. Нативный dialog удерживает фокус и обрабатывает Esc.
import { api } from './core/api.js'
import { toast } from './core/dom.js'

export function renameBook(work, onSaved) {
  const previous = document.activeElement
  const dialog = document.createElement('dialog')
  dialog.className = 'modal cp-modal'
  dialog.setAttribute('aria-labelledby', 'rename-heading')
  dialog.innerHTML = `<form>
    <div class="modal-head" id="rename-heading">Переименовать книгу</div>
    <div class="cp-body">
      <p><label>Название <input name="title" required maxlength="500" autofocus></label></p>
      <p><label>Автор <input name="author" maxlength="500"></label></p>
      <p role="alert" data-error></p>
      <div class="cp-actions">
        <button type="submit" class="btn-primary bp-btn">Сохранить</button>
        <button type="button" class="btn-ghost bp-btn" data-cancel>Отмена</button>
      </div>
    </div></form>`
  const form = dialog.querySelector('form')
  form.elements.title.value = work.title || ''
  form.elements.author.value = work.author || ''
  dialog.querySelector('[data-cancel]').onclick = () => dialog.close()
  dialog.addEventListener('close', () => {
    dialog.remove()
    if (previous?.isConnected && !previous.closest('[hidden]')) previous.focus()
    else document.querySelector('#bp-more')?.focus()
  }, { once: true })
  form.onsubmit = async (event) => {
    event.preventDefault()
    const title = form.elements.title.value.trim()
    if (!title) { form.elements.title.focus(); return }
    const submit = form.querySelector('[type=submit]')
    submit.disabled = true
    try {
      const saved = await api.put(`/api/library/${work.id}/identity`, {
        title, author: form.elements.author.value.trim(),
      })
      onSaved(saved)
      dialog.close()
      toast('Название и автор сохранены', 'ok')
    } catch (e) {
      dialog.querySelector('[data-error]').textContent = 'Не удалось сохранить: ' + e.message
    } finally { submit.disabled = false }
  }
  document.body.append(dialog)
  dialog.showModal()
}
