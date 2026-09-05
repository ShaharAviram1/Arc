/**
 * Minimal typed wrapper around fetch for the Arc API.
 *
 * Every call sends the session cookie (`credentials: 'include'`) because the
 * API authenticates with an HTTP-only session cookie. Non-2xx responses throw
 * an `ApiError` carrying the status and the parsed (or raw) response body.
 */

export type ApiErrorBody = unknown

export class ApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody

  constructor(status: number, body: ApiErrorBody, message?: string) {
    super(message ?? `Request failed with status ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

function isJson(response: Response): boolean {
  return (response.headers.get('content-type') ?? '').toLowerCase().includes('application/json')
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null
  const text = await response.text()
  if (text === '') return null
  if (!isJson(response)) return text
  try {
    return JSON.parse(text) as unknown
  } catch {
    return text
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  if (init?.body !== undefined && init.body !== null && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, {
    ...init,
    credentials: 'include',
    headers,
  })

  const body = await readBody(response)

  if (!response.ok) {
    throw new ApiError(
      response.status,
      body,
      `${init?.method ?? 'GET'} ${path} → ${response.status}`,
    )
  }

  return body as T
}
