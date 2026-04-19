/** Разбор тел ответа API: JSON от FastAPI, JSON от nginx-router (шлюз), либо HTML-заглушка. */

export async function readFetchJsonOrText(res) {
  const raw = await res.text()
  let json = null
  const t = raw.trim()
  if (t.startsWith('{') || t.startsWith('[')) {
    try {
      json = JSON.parse(raw)
    } catch (_) {
      /* оставляем json=null */
    }
  }
  return { raw, json }
}

/**
 * Человекочитаемое сообщение при !res.ok или при ошибке до разбора тела.
 * @param {Response} res
 * @param {{ raw: string, json: object|null }} body
 */
export function formatHttpFailureMessage(res, body) {
  const { raw, json } = body || { raw: '', json: null }
  if (json && json.source === 'nginx-router' && json.gateway) {
    const parts = ['Шлюз nginx → ядро']
    if (json.upstream_addr && json.upstream_addr !== '-') parts.push(`upstream ${json.upstream_addr}`)
    if (json.upstream_status && json.upstream_status !== '-') parts.push(`ответ upstream: ${json.upstream_status}`)
    if (json.upstream_response_time && json.upstream_response_time !== '-') {
      parts.push(`upstream_time ${json.upstream_response_time}`)
    }
    if (json.request_method && json.request_uri) parts.push(`${json.request_method} ${json.request_uri}`)
    if (json.detail) parts.push(String(json.detail))
    return parts.join(' · ')
  }
  if (res.status >= 500 && raw.trim().startsWith('<')) {
    return `Ошибка ${res.status}: HTML от шлюза (включите JSON-обработчик ошибок в nginx для /api/ или смотрите логи маршрутизатора)`
  }
  if (json && json.detail != null) return String(json.detail)
  if (json && json.message != null) return String(json.message)
  if (json && json.error != null) return String(json.error)
  if (raw && raw.length > 0 && raw.length < 400 && !raw.trim().startsWith('<')) return raw
  return `Ошибка ${res.status}`
}

/** true если статус — «сломался бэкенд / шлюз» и тело уже прочитано в body. */
export function isServerOrGatewayFailure(res) {
  return res.status >= 500 && res.status < 600
}
