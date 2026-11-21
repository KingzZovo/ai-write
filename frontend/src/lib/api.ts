// Empty string = relative URL, nginx proxies /api/ to backend
const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

const TOKEN_KEY = 'auth_token'

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string) {
    super(message)
    this.name = 'ApiError'
  }
}

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
  const headers: Record<string, string> = { ...authHeaders() }
  // Don't set Content-Type for FormData (browser sets multipart boundary automatically)
  if (!(options?.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }
  const { headers: optionHeaders, ...rest } = options ?? {}
  // PR-FIX-NO-STORE: bypass browser HTTP cache for all /api fetches.
  // Previously stale 422 / empty chapters responses lingered in browser cache
  // even after backend was fixed.
  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    cache: 'no-store',
    headers: { ...headers, ...(optionHeaders as Record<string, string> | undefined) },
  })
  if (res.status === 401) {
    clearToken()
    if (typeof window !== 'undefined') {
      window.location.href = '/login'
    }
    throw new ApiError('Unauthorized', 401)
  }
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = error?.detail
    if (typeof detail === 'string') {
      throw new ApiError(detail, res.status)
    }
    if (detail && typeof detail.message === 'string') {
      throw new ApiError(detail.message, res.status, typeof detail.code === 'string' ? detail.code : undefined)
    }
    if (detail && typeof detail.code === 'string') {
      throw new ApiError(detail.code, res.status, detail.code)
    }
    throw new ApiError(error?.message || 'API Error', res.status)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

/** Download a binary API response (e.g. /api/export/...) with the auth header,
 *  saving it via a temporary object URL. apiFetch is JSON-only. */
export async function apiDownload(path: string): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    headers: { ...authHeaders() },
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
    const detail = error?.detail
    throw new Error(typeof detail === 'string' ? detail : 'Download failed')
  }
  const blob = await res.blob()
  // Backend sends RFC 5987 filename*=UTF-8''... for Chinese titles.
  const cd = res.headers.get('content-disposition') || ''
  const match = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  const filename = match
    ? decodeURIComponent(match[1])
    : (path.split('/').pop() || 'download')
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export function apiSSE(
  path: string,
  body: Record<string, unknown>,
  onChunk: (text: string) => void,
  onDone: () => void,
  onEvent?: (event: Record<string, unknown>) => void,
  onError?: (err: Error) => void,
) {
  const controller = new AbortController()
  let finished = false
  const finish = (err?: Error) => {
    if (finished) return
    finished = true
    try {
      if (err) {
        console.error('SSE error:', err)
        onError?.(err)
      }
    } finally {
      // onDone must run even if onError throws: callers rely on it to clear
      // isGenerating / busy flags.
      onDone()
    }
  }

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
      finish(new Error('Unauthorized'))
      return
    }
    if (!res.ok || !res.body) {
      throw new Error(`SSE connection failed (${res.status})`)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let lastReviseRound: number | undefined

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          throw new Error('生成连接提前结束，未收到完成确认，请检查已收到的内容。')
        }
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || '' // keep incomplete last line for next chunk
        for (const rawLine of lines) {
          const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
          if (!line.startsWith('data:')) continue
          const data = line.slice(5).replace(/^ /, '')
          if (data === '[DONE]') {
            finish()
            return
          }
          let parsed: Record<string, unknown>
          try {
            const value: unknown = JSON.parse(data)
            if (value === null || typeof value !== 'object' || Array.isArray(value)) {
              throw new Error('invalid event')
            }
            parsed = value as Record<string, unknown>
          } catch {
            throw new Error('生成响应格式异常，请检查已收到的内容。')
          }
          // Application callback errors must not be converted into prose.
          const eventName = parsed.event ?? parsed.status
          // Named recovery events (e.g. revise_error/save_failed) may be
          // followed by persistence/acceptance events and belong to the caller.
          if (typeof parsed.error === 'string' &&
            (eventName === undefined || eventName === 'error' || eventName === 'failed')) {
            onEvent?.(parsed)
            throw new Error(parsed.error || '生成失败，请检查已收到的内容。')
          }
          if (typeof parsed.text === 'string') {
            if (
              typeof parsed.revise_round === 'number' &&
              parsed.revise_round !== lastReviseRound
            ) {
              lastReviseRound = parsed.revise_round
              onEvent?.({ event: 'revise_restart', revise_round: parsed.revise_round })
            }
            onChunk(parsed.text)
          } else {
            onEvent?.(parsed)
          }
        }
      }
    } finally {
      // Covers normal sentinel, malformed events, callback failure and abort.
      // Cancellation is best effort; final UI settlement must not wait on it.
      void reader.cancel().catch(() => {})
      reader.releaseLock()
    }
  }).catch((err) => {
    if (controller.signal.aborted && err !== null && typeof err === 'object' && err.name === 'AbortError') {
      finish() // user-initiated cancel still needs the UI to settle via onDone
      return
    }
    finish(err instanceof Error ? err : new Error(String(err)))
  })

  return controller
}
