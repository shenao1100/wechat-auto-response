export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`
    try { detail = (await response.json()).detail || detail } catch (_) { /* noop */ }
    throw new ApiError(detail, response.status)
  }
  return response.status === 204 ? null : response.json()
}

export const api = {
  health: () => request('/api/health'),
  settings: () => request('/api/settings'),
  saveGroups: (groups) => request('/api/settings/groups', { method: 'PUT', body: JSON.stringify(groups) }),
  evaluateGroup: (groupId) => request(`/api/groups/${encodeURIComponent(groupId)}/evaluate`, { method: 'POST' }),
  groups: () => request('/api/wechat/groups'),
  contacts: (q = '') => request(`/api/wechat/contacts?q=${encodeURIComponent(q)}`),
  status: () => request('/api/status'),
  prompts: () => request('/api/prompts'),
  prompt: (name) => request(`/api/prompts/${encodeURIComponent(name)}`),
  savePrompt: (name, content) => request(`/api/prompts/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ content }) }),
  deletePrompt: (name) => request(`/api/prompts/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  memories: () => request('/api/memories'),
  saveMemory: (payload) => request('/api/memories', { method: 'PUT', body: JSON.stringify(payload) }),
  deleteMemory: (key) => request(`/api/memories?key=${encodeURIComponent(key)}`, { method: 'DELETE' }),
  clarifications: (status = '') => request(`/api/clarifications${status ? `?status=${status}` : ''}`),
  answerClarification: (id, answer) => request(`/api/clarifications/${id}/answer`, { method: 'POST', body: JSON.stringify({ answer }) }),
  schedules: (groupId) => request(`/api/schedules?group_id=${encodeURIComponent(groupId)}`),
  runs: (limit = 50) => request(`/api/runs?limit=${limit}`),
  failed: () => request('/api/failed'),
  retryFailed: () => request('/api/failed/retry', { method: 'POST' }),
}
