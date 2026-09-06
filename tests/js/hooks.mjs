// Подменяем зависимости translate.js (DOM/сеть/шапка) заглушками, а сам
// translate.js берём НАСТОЯЩИЙ — иначе тест проверял бы копию, а не код.
const MAP = {
  './core/dom.js': './stub/dom.mjs',
  './core/api.js': './stub/api.mjs',
  './core/log.js': './stub/log.mjs',
  './core/state.js': './stub/state.mjs',
  './chrome.js': './stub/chrome.mjs',
}
export async function resolve(specifier, context, next) {
  if (MAP[specifier]) {
    return { url: new URL(MAP[specifier], import.meta.url).href, shortCircuit: true }
  }
  return next(specifier, context)
}
