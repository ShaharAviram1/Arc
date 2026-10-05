import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch, isOffline } from '@/lib/api'
import { offlineNow, resetNetwork } from '@/offline/network'
import { mockApi } from '@/test/apiMock'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('apiFetch', () => {
  it('sends credentials and JSON headers, and parses the JSON body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: 'ok' }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await apiFetch<{ status: string }>('/api/health')

    expect(result).toEqual({ status: 'ok' })
    expect(fetchMock).toHaveBeenCalledTimes(1)

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/health')
    expect(init.credentials).toBe('include')
    expect(new Headers(init.headers).get('Accept')).toBe('application/json')
  })

  it('sets Content-Type on requests that carry a body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 1 }, 201))
    vi.stubGlobal('fetch', fetchMock)

    await apiFetch('/api/list', { method: 'POST', body: JSON.stringify({ animeId: 1 }) })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(new Headers(init.headers).get('Content-Type')).toBe('application/json')
  })

  it('throws an ApiError carrying the status and body on a non-2xx response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'boom' }, 500))
    vi.stubGlobal('fetch', fetchMock)

    const error = await apiFetch('/api/health').catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(500)
    expect(apiError.body).toEqual({ detail: 'boom' })
  })

  it('passes a non-JSON body through untouched, on success and on failure', async () => {
    mockApi({
      'GET /api/health': { text: 'ok' },
      'GET /api/boom': { status: 502, text: '<html>Bad Gateway</html>' },
    })

    await expect(apiFetch<string>('/api/health')).resolves.toBe('ok')

    const error = await apiFetch('/api/boom').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).body).toBe('<html>Bad Gateway</html>')
  })

  it('returns null for a 204 response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(apiFetch('/api/logout', { method: 'POST' })).resolves.toBeNull()
  })
})

describe('offline detection (M18)', () => {
  afterEach(() => {
    resetNetwork()
  })

  it('a request with no response at all marks the app offline; the next answer clears it', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const error: unknown = await apiFetch('/api/home').catch((caught: unknown) => caught)
    expect(isOffline(error)).toBe(true)
    expect(offlineNow()).toBe(true)

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'no' }, 500)))
    const answered: unknown = await apiFetch('/api/home').catch((caught: unknown) => caught)
    expect(isOffline(answered)).toBe(false)
    expect(offlineNow()).toBe(false)
  })

  it.each([502, 503, 504])(
    'a %i from the gateway is offline too, so the strip agrees with isUnreachable (FR-S9)',
    async (status) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'down' }, status)))
      await apiFetch('/api/home').catch(() => undefined)
      expect(offlineNow()).toBe(true)
    },
  )

  it('an aborted request is not a network failure', async () => {
    const controller = new AbortController()
    controller.abort()
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new DOMException('aborted', 'AbortError')))

    await expect(apiFetch('/api/search', { signal: controller.signal })).rejects.toThrow()
    expect(offlineNow()).toBe(false)
  })
})
