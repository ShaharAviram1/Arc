/**
 * Minimal typed wrapper around fetch for the Arc API.
 *
 * Every call sends the session cookie (`credentials: 'include'`) because the
 * API authenticates with an HTTP-only session cookie. Non-2xx responses throw
 * an `ApiError` carrying the status and the parsed (or raw) response body.
 */

import { reportOffline, reportOnline } from '@/offline/network'

export type ApiErrorBody = unknown

/**
 * Whether a failure was "no response at all" rather than a response saying no.
 *
 * The whole of the offline story rests on this distinction (M18): a transport
 * failure is what the progress outbox catches and keeps for later, while an
 * `ApiError` is the server having an opinion, which a retry will not change.
 */
export function isOffline(error: unknown): boolean {
  return !(error instanceof ApiError)
}

/**
 * Whether the server could not be reached at all — no response, or the proxy
 * in front of it answering for a server that is down (502 / 503 / 504). This
 * is what the offline launch falls back on (FR-S9): a remembered user, a
 * remembered page. A 500 is the server having a bug and is never papered over.
 */
export function isUnreachable(error: unknown): boolean {
  if (isOffline(error)) return true
  return error instanceof ApiError && [502, 503, 504].includes(error.status)
}

export class ApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody
  /** Seconds from `Retry-After`, when the server sent one (429 / 503). */
  readonly retryAfter: number | null

  constructor(
    status: number,
    body: ApiErrorBody,
    message?: string,
    retryAfter: number | null = null,
  ) {
    super(message ?? `Request failed with status ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.retryAfter = retryAfter
  }
}

/**
 * `Retry-After` is either a delay in seconds or an HTTP date. Both are
 * normalised to whole seconds from now; anything unparseable becomes null.
 */
function parseRetryAfter(response: Response): number | null {
  const raw = response.headers.get('Retry-After')
  if (raw === null) return null

  const trimmed = raw.trim()
  if (trimmed === '') return null

  if (/^\d+$/.test(trimmed)) return Number(trimmed)

  const at = Date.parse(trimmed)
  if (Number.isNaN(at)) return null
  return Math.max(0, Math.ceil((at - Date.now()) / 1000))
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

  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'include',
      headers,
    })
  } catch (error) {
    // An abort is not a network failure: a cancelled search keystroke must not
    // put the app into its offline mode. Checked on the signal, because
    // `AbortError` is a `DOMException` in one runtime and an `Error` in another.
    if (init?.signal?.aborted === true) throw error
    // No response at all: no network, a dead tunnel, a server that is not
    // there. Recorded so the outbox and the shell know (`@/offline/network`).
    reportOffline()
    throw error
  }
  // A gateway answering for a server that is down is not "connected": the
  // offline strip agrees with `isUnreachable` (FR-S9).
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    reportOffline()
  } else {
    reportOnline()
  }

  const body = await readBody(response)

  if (!response.ok) {
    throw new ApiError(
      response.status,
      body,
      `${init?.method ?? 'GET'} ${path} → ${response.status}`,
      parseRetryAfter(response),
    )
  }

  return body as T
}
