import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { animeQueryKey } from '@/lib/anime'
import {
  formatMalDate,
  formatMalValue,
  MAL_WRITE_STATUS_CLASSES,
  MAL_WRITE_STATUS_LABELS,
  malErrorMessage,
  malLinkErrorMessage,
  malLogQueryKey,
  malStatusQueryKey,
  useMalImport,
  useMalLog,
  useMalPush,
  useMalStatus,
  useRevertMalWrite,
  useStartMalLink,
  useUnlinkMal,
} from '@/lib/mal'
import { createQueryClient } from '@/lib/queryClient'
import { MAL_LOG, MAL_STATUS_LINKED } from '@/test/animeFixtures'
import { mockApi, requestsMade } from '@/test/apiMock'
import { stubLocationAssign } from '@/test/location'

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children)
  }
}

const AUTHORIZE_URL = 'https://myanimelist.net/v1/oauth2/authorize?client_id=arc&state=abc'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('formatMalValue', () => {
  it('says the status the way the rest of the client says it', () => {
    expect(formatMalValue('status', 'on_hold')).toBe('On hold')
    expect(formatMalValue('status', 'watching')).toBe('Watching')
    // A status the client has never heard of is shown rather than swallowed.
    expect(formatMalValue('status', 'rewatching')).toBe('rewatching')
  })

  it('leaves numbers alone and reads an absent value as a dash', () => {
    expect(formatMalValue('progress', 12)).toBe('12')
    expect(formatMalValue('score', 0)).toBe('0')
    expect(formatMalValue('score', null)).toBe('—')
    expect(formatMalValue('score', undefined)).toBe('—')
    expect(formatMalValue('status', '')).toBe('—')
  })
})

describe('MAL_WRITE_STATUS_LABELS', () => {
  it('names every status the server can send, and calls `ok` synced', () => {
    // The wire values are `MalWriteStatus` in arc/models/enums.py; `done` is
    // not one of them.
    expect(Object.keys(MAL_WRITE_STATUS_LABELS).sort()).toEqual([
      'failed',
      'ok',
      'pending',
      'skipped',
    ])
    expect(MAL_WRITE_STATUS_LABELS.ok).toBe('Synced')
    expect(Object.keys(MAL_WRITE_STATUS_CLASSES).sort()).toEqual(
      Object.keys(MAL_WRITE_STATUS_LABELS).sort(),
    )
  })
})

describe('formatMalDate', () => {
  it('renders a timestamp and nothing at all for an absent or broken one', () => {
    expect(formatMalDate('2026-09-07T08:00:00Z')).toContain('2026')
    expect(formatMalDate(null)).toBe('—')
    expect(formatMalDate('')).toBe('—')
    expect(formatMalDate('not a date')).toBe('—')
  })
})

describe('malLinkErrorMessage', () => {
  it('explains the codes the callback can send, and quotes the ones it cannot', () => {
    expect(malLinkErrorMessage('access_denied')).toContain('declined')
    expect(malLinkErrorMessage('invalid_state')).toContain('expired')
    expect(malLinkErrorMessage('weird_thing')).toContain('weird_thing')
  })
})

describe('useMalStatus', () => {
  it('asks the server for the link state', async () => {
    const fetchMock = mockApi({ 'GET /api/mal/status': { body: MAL_STATUS_LINKED } })
    const client = createQueryClient()

    const { result } = renderHook(() => useMalStatus(), { wrapper: wrapperFor(client) })

    await waitFor(() => {
      expect(result.current.data).toEqual(MAL_STATUS_LINKED)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/mal/status'])
  })
})

describe('useMalLog', () => {
  it('sends the default limit and no status filter', async () => {
    const fetchMock = mockApi({ 'GET /api/mal/log?limit=50': { body: MAL_LOG } })

    const { result } = renderHook(() => useMalLog(), { wrapper: wrapperFor(createQueryClient()) })

    await waitFor(() => {
      expect(result.current.data).toEqual(MAL_LOG)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/mal/log?limit=50'])
  })

  it('sends the status filter and keys the cache on it', async () => {
    const fetchMock = mockApi({
      'GET /api/mal/log?limit=10&status=failed': { body: [MAL_LOG[1]] },
    })

    const { result } = renderHook(() => useMalLog({ limit: 10, status: 'failed' }), {
      wrapper: wrapperFor(createQueryClient()),
    })

    await waitFor(() => {
      expect(result.current.data).toHaveLength(1)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/mal/log?limit=10&status=failed'])
    expect(malLogQueryKey({ limit: 10, status: 'failed' })).not.toEqual(malLogQueryKey())
  })
})

describe('useStartMalLink', () => {
  it('sends the browser to the authorize URL the server minted', async () => {
    const fetchMock = mockApi({
      'POST /api/mal/link': { body: { authorize_url: AUTHORIZE_URL } },
    })
    const assign = stubLocationAssign()

    const { result } = renderHook(() => useStartMalLink(), {
      wrapper: wrapperFor(createQueryClient()),
    })
    await act(async () => {
      await result.current.mutateAsync()
    })

    expect(requestsMade(fetchMock)).toEqual(['POST /api/mal/link'])
    expect(assign).toHaveBeenCalledWith(AUTHORIZE_URL)
  })

  it('does not navigate when the server refuses', async () => {
    mockApi({ 'POST /api/mal/link': { status: 503, body: { detail: 'mal not configured' } } })
    const assign = stubLocationAssign()

    const { result } = renderHook(() => useStartMalLink(), {
      wrapper: wrapperFor(createQueryClient()),
    })
    await act(async () => {
      await result.current.mutateAsync().catch(() => undefined)
    })

    await waitFor(() => {
      expect(result.current.isError).toBe(true)
    })
    expect(assign).not.toHaveBeenCalled()
    expect(malErrorMessage(result.current.error)).toContain('client id')
  })
})

/** Every MAL mutation has to refresh both the MAL views and the show pages. */
const MUTATIONS = [
  { name: 'useUnlinkMal', hook: useUnlinkMal, request: 'DELETE /api/mal/link', status: 204 },
  { name: 'useMalImport', hook: useMalImport, request: 'POST /api/mal/import', status: 202 },
  { name: 'useMalPush', hook: useMalPush, request: 'POST /api/mal/push', status: 202 },
] as const

describe.each(MUTATIONS)('$name', ({ hook, request, status }) => {
  it('calls its endpoint and invalidates the MAL and anime caches', async () => {
    const fetchMock = mockApi({ [request]: { status } })
    const client = createQueryClient()
    client.setQueryData(malStatusQueryKey, MAL_STATUS_LINKED)
    client.setQueryData(animeQueryKey(1), { id: 1 })
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    const { result } = renderHook(() => hook(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await result.current.mutateAsync()
    })

    expect(requestsMade(fetchMock)).toContain(request)
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['mal'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['anime'] })
  })
})

describe('useRevertMalWrite', () => {
  it('posts to the write it is undoing and invalidates both caches', async () => {
    const fetchMock = mockApi({ 'POST /api/mal/log/2/revert': { status: 202 } })
    const client = createQueryClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    const { result } = renderHook(() => useRevertMalWrite(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await result.current.mutateAsync(2)
    })

    expect(requestsMade(fetchMock)).toEqual(['POST /api/mal/log/2/revert'])
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['mal'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['anime'] })
  })

  it('surfaces a 409 as "no longer possible"', async () => {
    mockApi({
      'POST /api/mal/log/2/revert': { status: 409, body: { detail: 'write is not revertible' } },
    })

    const { result } = renderHook(() => useRevertMalWrite(), {
      wrapper: wrapperFor(createQueryClient()),
    })
    await act(async () => {
      await result.current.mutateAsync(2).catch(() => undefined)
    })

    await waitFor(() => {
      expect(result.current.isError).toBe(true)
    })
    expect(malErrorMessage(result.current.error)).toBe('write is not revertible')
  })
})
