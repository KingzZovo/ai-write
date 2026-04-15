const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
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
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: controller.signal,
  }).then(async (res) => {
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
