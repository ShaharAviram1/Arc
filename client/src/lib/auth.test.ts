import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  authErrorMessage,
  authMeQueryKey,
  timezoneOptions,
  useLogin,
  useMe,
  useUpdateTimezone,
  type User,
} from '@/lib/auth'
import { createQueryClient } from '@/lib/queryClient'
import { homeQueryKey, scheduleQueryKey } from '@/lib/schedule'
import { EMPTY_HOME, SCHEDULE_PAGE } from '@/test/animeFixtures'
import { callTo, jsonBodyOf, mockApi, TEST_USER } from '@/test/apiMock'

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children)
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useMe', () => {
  it('resolves to null when the server says 401, instead of erroring', async () => {
    mockApi({ 'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } } })
    const client = createQueryClient()

    const { result } = renderHook(() => useMe(), { wrapper: wrapperFor(client) })

    await waitFor(() => {
      expect(result.current.isPending).toBe(false)
    })
    expect(result.current.data).toBeNull()
    expect(result.current.isError).toBe(false)
  })

  it('resolves to the user on 200', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    const client = createQueryClient()

    const { result } = renderHook(() => useMe(), { wrapper: wrapperFor(client) })

    await waitFor(() => {
      expect(result.current.data).toEqual(TEST_USER)
    })
  })
})

describe('useLogin', () => {
  it('writes the returned user into the me cache', async () => {
    mockApi({ 'POST /api/auth/login': { body: TEST_USER } })
    const client = createQueryClient()

    const { result } = renderHook(() => useLogin(), { wrapper: wrapperFor(client) })
    result.current.mutate({ email: 'viewer@example.com', password: 'hunter2hunter2' })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })
    expect(client.getQueryData<User>(authMeQueryKey)).toEqual(TEST_USER)
  })

  it('turns a 429 with Retry-After into a message naming the wait', async () => {
    mockApi({
      'POST /api/auth/login': {
        status: 429,
        body: { detail: 'too many requests' },
        headers: { 'Retry-After': '42' },
      },
    })
    const client = createQueryClient()

    const { result } = renderHook(() => useLogin(), { wrapper: wrapperFor(client) })
    result.current.mutate({ email: 'viewer@example.com', password: 'nope' })

    await waitFor(() => {
      expect(result.current.isError).toBe(true)
    })
    expect(authErrorMessage(result.current.error)).toBe(
      'Too many attempts, try again in 42 seconds.',
    )
  })

  it('turns a 401 into a wrong-credentials message', async () => {
    mockApi({ 'POST /api/auth/login': { status: 401, body: { detail: 'invalid credentials' } } })
    const client = createQueryClient()

    const { result } = renderHook(() => useLogin(), { wrapper: wrapperFor(client) })
    result.current.mutate({ email: 'viewer@example.com', password: 'nope' })

    await waitFor(() => {
      expect(result.current.isError).toBe(true)
    })
    expect(authErrorMessage(result.current.error)).toBe('Wrong email or password.')
  })
})

describe('useUpdateTimezone', () => {
  const TOKYO_USER: User = { ...TEST_USER, timezone: 'Asia/Tokyo' }

  it('PATCHes the zone, then re-seeds `me` and drops both aggregates', async () => {
    const fetchMock = mockApi({ 'PATCH /api/users/me': { body: TOKYO_USER } })
    const client = createQueryClient()
    // Two caches the server computes in the viewer's zone; both must go.
    client.setQueryData(scheduleQueryKey(), SCHEDULE_PAGE)
    client.setQueryData(homeQueryKey, EMPTY_HOME)

    const { result } = renderHook(() => useUpdateTimezone(), { wrapper: wrapperFor(client) })
    result.current.mutate('Asia/Tokyo')

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })

    expect(jsonBodyOf(callTo(fetchMock, '/api/users/me'))).toEqual({ timezone: 'Asia/Tokyo' })
    expect(client.getQueryData<User>(authMeQueryKey)).toEqual(TOKYO_USER)
    expect(client.getQueryState(scheduleQueryKey())?.isInvalidated).toBe(true)
    expect(client.getQueryState(homeQueryKey)?.isInvalidated).toBe(true)
  })
})

describe('timezoneOptions', () => {
  it('offers the browser’s own zone list', () => {
    const zones = timezoneOptions('Europe/Berlin')

    expect(zones).toContain('Europe/Berlin')
    expect(zones).toContain('Asia/Tokyo')
  })

  it('keeps a zone the browser has never heard of selectable', () => {
    expect(timezoneOptions('Mars/Olympus_Mons')[0]).toBe('Mars/Olympus_Mons')
  })
})

describe('authErrorMessage', () => {
  it('falls back to a transport message for a non-API error', () => {
    expect(authErrorMessage(new TypeError('Failed to fetch'))).toBe(
      'Could not reach the server. Check your connection and try again.',
    )
  })
})
