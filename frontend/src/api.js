// Every call goes to the same origin: FastAPI serves the built bundle, and the
// Vite dev server proxies /api to it. One code path for both modes.

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail || detail
    } catch {
      // non-JSON error body; the status text is all we have
    }
    throw new Error(detail)
  }
  return response.json()
}

export const api = {
  health: () => request('/health'),

  // chat
  me: () => request('/api/me'),
  currentConversation: () => request('/api/conversations/current'),
  conversation: (id) => request(`/api/conversations/${id}`),
  newConversation: () => request('/api/conversations/new', { method: 'POST' }),
  send: (message, conversationId) =>
    request('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ message, conversation_id: conversationId })
    }),

  // admin
  pending: (status) => request(`/api/admin/pending${status ? `?status=${status}` : ''}`),
  pendingDetail: (id) => request(`/api/admin/pending/${id}`),
  approve: (id, note = '') =>
    request(`/api/admin/pending/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ note })
    }),
  reject: (id, note = '') =>
    request(`/api/admin/pending/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ note })
    }),
  metrics: () => request('/api/admin/metrics'),
  activity: () => request('/api/admin/activity?limit=30'),
  apiLogs: () => request('/api/admin/api-logs?limit=120'),
  llmCalls: () => request('/api/admin/llm-calls?limit=80'),
  auditLogs: () => request('/api/admin/audit-logs?limit=120'),
  emails: () => request('/api/admin/emails'),
  tickets: () => request('/api/admin/tickets'),
  orders: () => request('/api/admin/orders'),
  customer: () => request('/api/admin/customer'),
  knowledge: () => request('/api/admin/knowledge'),
  knowledgeSearch: (q) => request(`/api/admin/knowledge/search?q=${encodeURIComponent(q)}`),
  policyRules: () => request('/api/admin/policy-rules'),
  updatePolicyRule: (key, value) =>
    request(`/api/admin/policy-rules/${key}`, {
      method: 'PATCH',
      body: JSON.stringify({ value_int: value })
    })
}

// One SSE subscription per view. `*` is the firehose the admin page listens on
// so an approval shows up without a refresh.
export function subscribe(conversationId, handlers = {}) {
  const source = new EventSource(`/api/stream?conversation_id=${encodeURIComponent(conversationId)}`)
  for (const [type, handler] of Object.entries(handlers)) {
    source.addEventListener(type, (event) => {
      try {
        handler(JSON.parse(event.data))
      } catch {
        handler({})
      }
    })
  }
  return source
}

export function money(cents) {
  if (cents === null || cents === undefined) return '—'
  return `$${(cents / 100).toFixed(2)}`
}

export function shortTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function stamp(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}
