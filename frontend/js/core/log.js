// Единая точка для «проглоченных» сбоев: операция не критична для UI
// (остаёмся устойчивыми и НЕ показываем ошибку пользователю), но и не прячем
// её молча — пишем в консоль с контекстом, чтобы сбой было видно при отладке.
export function logErr(context, err) {
  const detail = err && err.status
    ? `${err.status} ${err.statusText}`
    : (err && err.message) || err
  console.warn(`[reader] ${context}:`, detail)
}
