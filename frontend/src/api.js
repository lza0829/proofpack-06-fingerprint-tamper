async function toError(r) {
  const text = await r.text()
  try {
    const body = JSON.parse(text)
    const d = body.detail
    if (d && typeof d === 'object' && d.message) {
      return new Error(d.message + (d.reasons?.length ? `（${d.reasons.join('；')}）` : ''))
    }
    if (typeof d === 'string') return new Error(d)
  } catch (e) {
    /* 非 JSON 响应，回退原文 */
  }
  return new Error(text)
}

export async function getJSON(url) {
  const r = await fetch(url)
  if (!r.ok) throw await toError(r)
  return r.json()
}

export async function postJSON(url, body = {}) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw await toError(r)
  return r.json()
}

export async function putJSON(url, body = {}) {
  const r = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw await toError(r)
  return r.json()
}
