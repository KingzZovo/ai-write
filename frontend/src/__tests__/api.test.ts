import { describe, it, expect, vi, beforeEach } from 'vitest'

describe('apiFetch', () => {
  const mockFetch = vi.fn()

  beforeEach(() => {
    vi.resetModules()
    mockFetch.mockReset()
    vi.stubGlobal('fetch', mockFetch)
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => key === 'auth_token' ? 'test-token' : null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })
  })

  it('adds Authorization header from localStorage', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ data: 'test' }),
    })

    const { apiFetch } = await import('@/lib/api')
    await apiFetch('/api/projects')

    expect(mockFetch).toHaveBeenCalledWith(
      '/api/projects',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer test-token',
        }),
      }),
    )
  })

  it('sets Content-Type to application/json by default', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    })

    const { apiFetch } = await import('@/lib/api')
    await apiFetch('/api/projects')

    expect(mockFetch).toHaveBeenCalledWith(
      '/api/projects',
      expect.objectContaining({
        headers: expect.objectContaining({
          'Content-Type': 'application/json',
        }),
      }),
    )
  })

  it('throws on non-ok response with detail message', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ detail: 'Bad request body' }),
    })

    const { apiFetch } = await import('@/lib/api')
    await expect(apiFetch('/api/projects')).rejects.toThrow('Bad request body')
  })

  it('clears token and throws on 401', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: () => Promise.resolve({ detail: 'Unauthorized' }),
    })

    const { apiFetch } = await import('@/lib/api')
    await expect(apiFetch('/api/projects')).rejects.toThrow('Unauthorized')
    expect(localStorage.removeItem).toHaveBeenCalledWith('auth_token')
  })

  it('returns undefined for 204 responses', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 204,
      json: () => Promise.resolve(null),
    })

    const { apiFetch } = await import('@/lib/api')
    const result = await apiFetch('/api/chapters/123')
    expect(result).toBeUndefined()
  })
})

describe('apiDownload', () => {
  const mockFetch = vi.fn()

  beforeEach(() => {
    vi.resetModules()
    mockFetch.mockReset()
    vi.stubGlobal('fetch', mockFetch)
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => key === 'auth_token' ? 'test-token' : null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })
  })

  it('sends auth header and saves blob under the RFC 5987 filename', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: {
        get: (name: string) =>
          name === 'content-disposition'
            ? "attachment; filename=\"x.txt\"; filename*=UTF-8''%E6%88%91%E7%9A%84%E5%B0%8F%E8%AF%B4.txt"
            : null,
      },
      blob: () => Promise.resolve(new Blob(['第1章'])),
    })
    const createObjectURL = vi.fn(() => 'blob:mock')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    const { apiDownload } = await import('@/lib/api')
    const anchors: string[] = []
    const origAppend = document.body.appendChild.bind(document.body)
    vi.spyOn(document.body, 'appendChild').mockImplementation((node) => {
      if (node instanceof HTMLAnchorElement) anchors.push(node.download)
      return origAppend(node)
    })
    await apiDownload('/api/export/projects/p1.txt')

    expect(mockFetch).toHaveBeenCalledWith(
      '/api/export/projects/p1.txt',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
      }),
    )
    expect(anchors).toEqual(['我的小说.txt'])
    expect(click).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock')
    click.mockRestore()
    vi.mocked(document.body.appendChild).mockRestore()
  })

  it('throws detail message on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: 'project not found' }),
    })
    const { apiDownload } = await import('@/lib/api')
    await expect(apiDownload('/api/export/projects/p1.txt')).rejects.toThrow('project not found')
  })
})

describe('apiSSE', () => {
  const mockFetch = vi.fn()

  beforeEach(() => {
    vi.resetModules()
    mockFetch.mockReset()
    vi.stubGlobal('fetch', mockFetch)
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })
  })

  function mockSSEResponse(events: Record<string, unknown>[]) {
    const encoder = new TextEncoder()
    const payload = events
      .map((e) => `data: ${JSON.stringify(e)}\n`)
      .join('') + 'data: [DONE]\n'
    let sent = false
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: () => {
            if (sent) return Promise.resolve({ done: true, value: undefined })
            sent = true
            return Promise.resolve({ done: false, value: encoder.encode(payload) })
          },
          cancel: () => Promise.resolve(),
        }),
      },
    })
  }

  function runSSE(events: Record<string, unknown>[]) {
    mockSSEResponse(events)
    const chunks: string[] = []
    const emitted: Record<string, unknown>[] = []
    return new Promise<{ chunks: string[]; emitted: Record<string, unknown>[] }>(
      async (resolve, reject) => {
        const { apiSSE } = await import('@/lib/api')
        apiSSE(
          '/api/generate/chapter',
          {},
          (text) => chunks.push(text),
          () => resolve({ chunks, emitted }),
          (evt) => emitted.push(evt),
          (err) => reject(err),
        )
      },
    )
  }

  it('routes text chunks to onChunk and other events to onEvent', async () => {
    const { chunks, emitted } = await runSSE([
      { text: 'hello ' },
      { text: 'world' },
      { event: 'saved' },
    ])
    expect(chunks).toEqual(['hello ', 'world'])
    expect(emitted).toEqual([{ event: 'saved' }])
  })

  it('emits revise_restart before the first chunk of each new revise round', async () => {
    const order: string[] = []
    mockSSEResponse([
      { text: 'original ' },
      { text: 'draft' },
      { text: 'revised v1 part1 ', revise_round: 1 },
      { text: 'revised v1 part2', revise_round: 1 },
      { text: 'revised v2', revise_round: 2 },
    ])
    await new Promise<void>(async (resolve, reject) => {
      const { apiSSE } = await import('@/lib/api')
      apiSSE(
        '/api/generate/chapter',
        {},
        (text) => order.push(`chunk:${text}`),
        () => resolve(),
        (evt) => order.push(`event:${evt.event}:${evt.revise_round}`),
        (err) => reject(err),
      )
    })
    expect(order).toEqual([
      'chunk:original ',
      'chunk:draft',
      'event:revise_restart:1',
      'chunk:revised v1 part1 ',
      'chunk:revised v1 part2',
      'event:revise_restart:2',
      'chunk:revised v2',
    ])
  })
})
