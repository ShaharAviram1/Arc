import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch } from '@/lib/api'
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
