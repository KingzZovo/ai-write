import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

vi.stubGlobal('localStorage', {
  getItem: vi.fn(() => 'test-token'),
  setItem: vi.fn(),
  removeItem: vi.fn(),
})

describe('apiFetch', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    vi.resetModules()
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
      expect.stringContaining('/api/projects'),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer test-token',
        }),
      }),
    )
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ detail: 'Server Error' }),
    })

    const { apiFetch } = await import('@/lib/api')
    await expect(apiFetch('/api/projects')).rejects.toThrow('Server Error')
  })

  it('redirects to /login on 401', async () => {
    const mockLocation = { href: '' }
    vi.stubGlobal('window', { location: mockLocation })

    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: () => Promise.resolve({ detail: 'Unauthorized' }),
    })

    const { apiFetch } = await import('@/lib/api')
    await expect(apiFetch('/api/projects')).rejects.toThrow('Unauthorized')
    expect(mockLocation.href).toBe('/login')
  })
})
