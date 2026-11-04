import { vi } from 'vitest'
import type { User } from '@/lib/auth'

export interface MockResponse {
  status?: number
  body?: unknown
  /**
   * A raw, non-JSON body (sent as `text/plain` unless `headers` says
   * otherwise). Takes precedence over `body`; this is how a proxy or gateway
   * answers, and what `api.ts` falls back to returning verbatim.
   */
  text?: string
  headers?: Record<string, string>
}

/** Keyed by `"<METHOD> <path>"`, e.g. `"POST /api/auth/login"`. */
export type MockRoutes = Record<string, MockResponse>

export const TEST_USER: User = {
  id: 1,
  email: 'viewer@example.com',
  role: 'user',
  timezone: 'Europe/Berlin',
  created_at: '2026-09-01T10:00:00Z',
}

export const TEST_ADMIN: User = { ...TEST_USER, id: 2, email: 'admin@example.com', role: 'admin' }

function pathOf(input: string | URL | Request): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.href
  return input.url
}

function toResponse(mock: MockResponse): Response {
  const status = mock.status ?? 200
  if (mock.text !== undefined) {
    return new Response(mock.text, {
      status,
      headers: { 'Content-Type': 'text/plain; charset=utf-8', ...mock.headers },
    })
  }
  if (status === 204 || mock.body === undefined) {
    return new Response(null, { status, headers: mock.headers })
  }
  return new Response(JSON.stringify(mock.body), {
    status,
    headers: { 'Content-Type': 'application/json', ...mock.headers },
  })
}

/**
 * Stubs `fetch` with a tiny router. Unmapped requests resolve to 404 with a
 * loud detail so a missing route shows up as a test failure, not a hang.
 */
export function mockApi(routes: MockRoutes) {
  const fetchMock = vi.fn((input: string | URL | Request, init?: RequestInit) => {
    const path = pathOf(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    const mock = routes[`${method} ${path}`]
    if (mock === undefined) {
      return Promise.resolve(
        toResponse({ status: 404, body: { detail: `unmocked ${method} ${path}` } }),
      )
    }
    return Promise.resolve(toResponse(mock))
  })

  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Requests the mock actually received, as `"<METHOD> <path>"` strings. */
export function requestsMade(fetchMock: ReturnType<typeof mockApi>): string[] {
  return fetchMock.mock.calls.map(
    ([input, init]) => `${(init?.method ?? 'GET').toUpperCase()} ${pathOf(input)}`,
  )
}

/** The JSON body a recorded call sent, or `{}` when it sent none. */
export function jsonBodyOf(init: RequestInit | undefined): Record<string, unknown> {
  const body = init?.body
  if (typeof body !== 'string') return {}
  return JSON.parse(body) as Record<string, unknown>
}

/** The first recorded call to `path`, as `[path, init]`. */
export function callTo(
  fetchMock: ReturnType<typeof mockApi>,
  path: string,
): RequestInit | undefined {
  const call = fetchMock.mock.calls.find(([input]) => pathOf(input) === path)
  return call?.[1]
}
