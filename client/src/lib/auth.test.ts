import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { authErrorMessage, authMeQueryKey, useLogin, useMe, type User } from '@/lib/auth'
import { createQueryClient } from '@/lib/queryClient'
import { mockApi, TEST_USER } from '@/test/apiMock'

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

describe('authErrorMessage', () => {
  it('falls back to a transport message for a non-API error', () => {
    expect(authErrorMessage(new TypeError('Failed to fetch'))).toBe(
      'Could not reach the server. Check your connection and try again.',
    )
  })
})
