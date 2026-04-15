// Empty string = relative URL, nginx proxies /api/ to backend
const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

const TOKEN_KEY = 'auth_token'

export function getToken(): string | null {
  if (typeof window === 'undefined') return null
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  if (token) {
    return { Authorization: `Bearer ${token}` }
  }
  return {}
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...options?.headers },
    ...options,
  })
  if (res.status === 401) {
    clearToken()
    if (typeof window !== 'undefined') {
      window.location.href = '/login'
    }
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(error.detail || 'API Error')
  }
  return res.json()
}

export function apiSSE(path: string, body: Record<string, unknown>, onChunk: (text: string) => void, onDone: () => void) {
  const controller = new AbortController()

  fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
    signal: controller.signal,
  }).then(async (res) => {
    if (res.status === 401) {
      clearToken()
      if (typeof window !== 'undefined') {
        window.location.href = '/login'
      }
      return
    }
    if (!res.ok || !res.body) {
      throw new Error('SSE connection failed')
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const text = decoder.decode(value, { stream: true })
      const lines = text.split('\n')
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6)
          if (data === '[DONE]') {
            onDone()
            return
          }
          try {
            const parsed = JSON.parse(data)
            if (parsed.text) onChunk(parsed.text)
          } catch {
            onChunk(data)
          }
        }
      }
    }
    onDone()
  }).catch((err) => {
    if (err.name !== 'AbortError') console.error('SSE error:', err)
  })

  return controller
}
