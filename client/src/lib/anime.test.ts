import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import {
  anilistUrl,
  animeQueryKey,
  catalogErrorMessage,
  listErrorMessage,
  listQueryKey,
  malUrl,
  useAnimeSearch,
  useRemoveListEntry,
  useSetListEntry,
  type AnimeDetail,
} from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import {
  FRIEREN,
  FRIEREN_DETAIL,
  FRIEREN_DETAIL_ON_LIST,
  listEntry,
  SEARCH_PAGE_1,
} from '@/test/animeFixtures'
import { mockApi, requestsMade } from '@/test/apiMock'

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children)
  }
}

/** The show page's cache, primed as if the user had just opened it. */
function seedShowCache(client: QueryClient): void {
  client.setQueryData<AnimeDetail>(animeQueryKey(FRIEREN.id), FRIEREN_DETAIL)
  client.setQueryData(listQueryKey(), [])
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('listErrorMessage', () => {
  it('reads a 404 as "the entry is not there", which is what DELETE means by it', () => {
    expect(listErrorMessage(new ApiError(404, { detail: 'entry not found' }))).toBe(
      'Not on your list.',
    )
  })

  it("surfaces the server's own detail for a 422", () => {
    expect(listErrorMessage(new ApiError(422, { detail: 'status is required' }))).toBe(
      'status is required',
    )
  })

  it('falls back to a reachability message for anything that is not an ApiError', () => {
    expect(listErrorMessage(new Error('offline'))).toBe('Could not reach the server.')
  })
})

describe('catalogErrorMessage', () => {
  it('names the outage for the 502 that means both sources are down', () => {
    expect(
      catalogErrorMessage(new ApiError(502, { detail: 'catalogue is unavailable' }), 'fallback'),
    ).toBe('The catalogue is unavailable right now. Try again in a few minutes.')
  })

  it('leaves every other failure to the caller’s own wording', () => {
    expect(catalogErrorMessage(new ApiError(500, { detail: 'boom' }), 'fallback')).toBe('fallback')
    expect(catalogErrorMessage(new Error('offline'), 'fallback')).toBe('fallback')
  })
})

describe('catalogue links', () => {
  it('builds the public page for each source', () => {
    expect(anilistUrl(154587)).toBe('https://anilist.co/anime/154587')
    expect(malUrl(52991)).toBe('https://myanimelist.net/anime/52991')
  })
})

describe('useAnimeSearch', () => {
  it('does not call the API for a query shorter than two characters', () => {
    const fetchMock = mockApi({})
    const client = createQueryClient()

    const { result } = renderHook(() => useAnimeSearch('f'), { wrapper: wrapperFor(client) })

    expect(result.current.fetchStatus).toBe('idle')
    expect(result.current.data).toBeUndefined()
    expect(requestsMade(fetchMock)).toHaveLength(0)
  })

  it('calls the API once the query is long enough', async () => {
    const fetchMock = mockApi({
      'GET /api/anime/search?q=fr&page=1': { body: SEARCH_PAGE_1 },
    })
    const client = createQueryClient()

    const { result } = renderHook(() => useAnimeSearch('fr'), { wrapper: wrapperFor(client) })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/anime/search?q=fr&page=1'])
  })

  it('does not retry a failed search — every attempt costs an AniList call', async () => {
    const fetchMock = mockApi({
      'GET /api/anime/search?q=fr&page=1': {
        status: 502,
        body: { detail: 'catalogue is unavailable' },
      },
    })
    const client = createQueryClient()

    const { result } = renderHook(() => useAnimeSearch('fr'), { wrapper: wrapperFor(client) })

    await waitFor(() => {
      expect(result.current.isError).toBe(true)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/anime/search?q=fr&page=1'])
  })
})

describe('useSetListEntry', () => {
  it('writes the new entry into the anime cache and invalidates the list', async () => {
    const fetchMock = mockApi({
      [`PUT /api/list/${FRIEREN.id}`]: { body: listEntry({ status: 'watching', progress: 4 }) },
    })
    const client = createQueryClient()
    seedShowCache(client)

    const { result } = renderHook(() => useSetListEntry(), { wrapper: wrapperFor(client) })
    act(() => {
      result.current.mutate({ animeId: FRIEREN.id, status: 'watching' })
    })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })

    const cached = client.getQueryData<AnimeDetail>(animeQueryKey(FRIEREN.id))
    expect(cached?.list_status).toBe('watching')
    expect(cached?.list_entry).toMatchObject({ status: 'watching', progress: 4 })
    expect(client.getQueryState(listQueryKey())?.isInvalidated).toBe(true)
    expect(requestsMade(fetchMock)).toEqual([`PUT /api/list/${FRIEREN.id}`])
  })
})

describe('useRemoveListEntry', () => {
  it('clears the entry from the anime cache', async () => {
    mockApi({ [`DELETE /api/list/${FRIEREN.id}`]: { status: 204 } })
    const client = createQueryClient()
    client.setQueryData<AnimeDetail>(animeQueryKey(FRIEREN.id), FRIEREN_DETAIL_ON_LIST)
    client.setQueryData(listQueryKey(), [])

    const { result } = renderHook(() => useRemoveListEntry(), { wrapper: wrapperFor(client) })
    act(() => {
      result.current.mutate(FRIEREN.id)
    })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })

    const cached = client.getQueryData<AnimeDetail>(animeQueryKey(FRIEREN.id))
    expect(cached?.list_status).toBeNull()
    expect(cached?.list_entry).toBeNull()
    expect(client.getQueryState(listQueryKey())?.isInvalidated).toBe(true)
  })
})
