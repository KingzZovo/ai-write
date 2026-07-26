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
