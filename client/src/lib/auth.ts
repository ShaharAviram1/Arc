/**
 * Auth state for the Arc client (spec §2, roadmap M2).
 *
 * The session lives in an HTTP-only cookie the client never sees, so "am I
 * logged in?" is a server question: `GET /api/auth/me` is the single source of
 * truth, cached under `['auth', 'me']`. A 401 there is not an error — it is
 * the answer "logged out" — so the query resolves to `null` instead of
 * throwing, and every guard can branch on `undefined` (loading) / `null`
 * (logged out) / `User` (logged in).
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError, apiFetch } from '@/lib/api'
import { HOME_QUERY_KEY, SCHEDULE_QUERY_KEY } from '@/lib/schedule'

export type Role = 'admin' | 'user'

export interface User {
  id: number
  email: string
  role: Role
  timezone: string
  created_at: string
}

/** `GET /api/invites/{token}`: an unredeemed invite. `email` may be unset. */
export interface Invite {
  email: string | null
  expires_at: string
}

export interface LoginInput {
  email: string
  password: string
}

export interface AcceptInviteInput {
  email?: string
  password: string
  timezone?: string
}

export const authMeQueryKey = ['auth', 'me'] as const

export function inviteQueryKey(token: string): readonly ['invite', string] {
  return ['invite', token] as const
}

/** Minimum password length; must stay in step with the server's validator. */
export const MIN_PASSWORD_LENGTH = 10

/** The `detail` of an API error, when the server sent a plain-string one. */
export function errorDetail(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const body: unknown = error.body
  if (typeof body !== 'object' || body === null || !('detail' in body)) return null
  const detail: unknown = body.detail
  return typeof detail === 'string' ? detail : null
}

/** True when the error is an ApiError with this exact status. */
export function isStatus(error: unknown, status: number): boolean {
  return error instanceof ApiError && error.status === status
}

/** The server's `detail` for "an account with this address already exists". */
export const EMAIL_TAKEN_DETAIL = 'email already registered'

/**
 * The one 409 a person can act on: the address is taken, so signing in is the
 * next step. The other 409 (`email does not match invite`) is a bug in the
 * caller, not something to offer a sign-in link for.
 */
export function isEmailTaken(error: unknown): boolean {
  return isStatus(error, 409) && errorDetail(error) === EMAIL_TAKEN_DETAIL
}

/**
 * A message a person can act on. Anything that is not an `ApiError` is a
 * transport failure (offline, server down, CORS), which reads as generic.
 */
export function authErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return 'Could not reach the server. Check your connection and try again.'
  }

  switch (error.status) {
    case 401:
      return 'Wrong email or password.'
    case 404:
      return 'This invite link is invalid or has already been used.'
    case 409:
      return isEmailTaken(error)
        ? 'That email is already registered.'
        : (errorDetail(error) ?? 'That does not match this invite.')
    case 422:
      return errorDetail(error) ?? 'Check the details you entered and try again.'
    case 429:
      return error.retryAfter === null
        ? 'Too many attempts, try again in a little while.'
        : `Too many attempts, try again in ${error.retryAfter} seconds.`
    default:
      return 'Something went wrong. Please try again.'
  }
}

/** The browser's IANA timezone, sent silently when accepting an invite. */
export function browserTimezone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
  } catch {
    return undefined
  }
}

/**
 * A short, spread-out zone list for a browser without
 * `Intl.supportedValuesOf` — one common city per broad offset, enough that
 * nobody is stuck with a zone hours away from their own.
 */
const FALLBACK_TIMEZONES: readonly string[] = [
  'UTC',
  'Pacific/Honolulu',
  'America/Anchorage',
  'America/Los_Angeles',
  'America/Denver',
  'America/Chicago',
  'America/New_York',
  'America/Sao_Paulo',
  'Europe/London',
  'Europe/Berlin',
  'Europe/Athens',
  'Europe/Moscow',
  'Africa/Lagos',
  'Africa/Nairobi',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Bangkok',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Australia/Sydney',
  'Pacific/Auckland',
]

/**
 * IANA zones to offer, with `current` guaranteed to be among them: a zone the
 * browser has never heard of is still the one the account is set to, and a
 * select that cannot show its own value would silently change it on save.
 */
export function timezoneOptions(current: string): string[] {
  let zones: string[]
  try {
    zones =
      typeof Intl.supportedValuesOf === 'function'
        ? Intl.supportedValuesOf('timeZone')
        : [...FALLBACK_TIMEZONES]
  } catch {
    zones = [...FALLBACK_TIMEZONES]
  }
  return current !== '' && !zones.includes(current) ? [current, ...zones] : zones
}

/** `undefined` while loading, `null` when logged out, the user when logged in. */
export function useMe(): UseQueryResult<User | null, Error> {
  return useQuery<User | null, Error>({
    queryKey: authMeQueryKey,
    queryFn: async () => {
      try {
        return await apiFetch<User>('/api/auth/me')
      } catch (error) {
        if (isStatus(error, 401)) return null
        throw error
      }
    },
    retry: false,
  })
}

export function useLogin(): UseMutationResult<User, Error, LoginInput> {
  const queryClient = useQueryClient()

  return useMutation<User, Error, LoginInput>({
    mutationFn: (input) =>
      apiFetch<User>('/api/auth/login', { method: 'POST', body: JSON.stringify(input) }),
    onSuccess: (user) => {
      queryClient.setQueryData(authMeQueryKey, user)
    },
  })
}

/**
 * Change the viewer's own timezone (spec §4.1 FR-C3).
 *
 * The zone is not a formatting preference the client applies: the server
 * groups the schedule into weekday columns and works out air times in it, so
 * changing it changes those *responses*. Hence both aggregates are invalidated
 * rather than patched — there is nothing sensible to patch them to, and only
 * the server can re-derive them.
 */
export function useUpdateTimezone(): UseMutationResult<User, Error, string> {
  const queryClient = useQueryClient()

  return useMutation<User, Error, string>({
    mutationFn: (timezone) =>
      apiFetch<User>('/api/users/me', {
        method: 'PATCH',
        body: JSON.stringify({ timezone }),
      }),
    onSuccess: (user) => {
      queryClient.setQueryData(authMeQueryKey, user)
      void queryClient.invalidateQueries({ queryKey: [SCHEDULE_QUERY_KEY] })
      void queryClient.invalidateQueries({ queryKey: [HOME_QUERY_KEY] })
    },
  })
}

export function useLogout(): UseMutationResult<null, Error, void> {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  /**
   * Leave the session behind on the client. Runs whatever the server said:
   * once a person has asked to log out, a half-logged-in client — cached
   * responses on screen, `me` still set — is the one state we must not be in.
   * The cookie may or may not be gone; the next request will find out.
   */
  function endSession() {
    // Drop every cached response: none of it belongs to the next user.
    queryClient.clear()
    queryClient.setQueryData(authMeQueryKey, null)
    void navigate('/login', { replace: true })
  }

  return useMutation<null, Error, void>({
    mutationFn: async () => {
      try {
        return await apiFetch<null>('/api/auth/logout', { method: 'POST' })
      } catch (error) {
        // 401 is not a failure here: the session was already gone.
        if (isStatus(error, 401)) return null
        throw error
      }
    },
    onSuccess: endSession,
    // The mutation still settles as an error, so `logout.error` stays
    // available (via `authErrorMessage`) for a caller that wants to say so.
    onError: endSession,
  })
}

export function useInvite(token: string): UseQueryResult<Invite, Error> {
  return useQuery<Invite, Error>({
    queryKey: inviteQueryKey(token),
    queryFn: () => apiFetch<Invite>(`/api/invites/${encodeURIComponent(token)}`),
    retry: false,
    enabled: token !== '',
  })
}

/** Accepting an invite creates the account and logs it in, so `me` is set. */
export function useAcceptInvite(token: string): UseMutationResult<User, Error, AcceptInviteInput> {
  const queryClient = useQueryClient()

  return useMutation<User, Error, AcceptInviteInput>({
    mutationFn: (input) =>
      apiFetch<User>(`/api/invites/${encodeURIComponent(token)}/accept`, {
        method: 'POST',
        body: JSON.stringify(input),
      }),
    onSuccess: (user) => {
      queryClient.setQueryData(authMeQueryKey, user)
    },
  })
}
