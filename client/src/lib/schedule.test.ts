import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRemoveListEntry, useSetListEntry, type ListStatus } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import {
  homeQueryKey,
  isFollowing,
  parseSeason,
  parseYear,
  scheduleQueryKey,
  seasonLabel,
  useHome,
  useSchedule,
  weekdayInTimezone,
  WEEKDAY_LABELS,
  type HomePage,
  type SchedulePage,
} from '@/lib/schedule'
import { FRIEREN, FRIEREN_SPECIAL, HOME_PAGE, listEntry, SCHEDULE_PAGE } from '@/test/animeFixtures'
import { mockApi, requestsMade } from '@/test/apiMock'

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children)
  }
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('seasonLabel', () => {
  it('reads the way a person names a season', () => {
    expect(seasonLabel(2026, 'FALL')).toBe('Fall 2026')
    expect(seasonLabel(2027, 'WINTER')).toBe('Winter 2027')
    expect(seasonLabel(2026, 'SPRING')).toBe('Spring 2026')
    expect(seasonLabel(2026, 'SUMMER')).toBe('Summer 2026')
  })
})

describe('WEEKDAY_LABELS', () => {
  it('starts on Monday, matching ScheduleDay.weekday', () => {
    expect(WEEKDAY_LABELS).toHaveLength(7)
    expect(WEEKDAY_LABELS[0]).toBe('Monday')
    expect(WEEKDAY_LABELS[6]).toBe('Sunday')
  })
})

describe('parseSeason / parseYear', () => {
  it('accepts what the server accepts and rejects the rest', () => {
    expect(parseSeason('FALL')).toBe('FALL')
    expect(parseSeason('fall')).toBe('FALL')
    expect(parseSeason('AUTUMN')).toBeUndefined()
    expect(parseSeason(null)).toBeUndefined()

    expect(parseYear('2026')).toBe(2026)
    expect(parseYear('26')).toBeUndefined()
    expect(parseYear('nope')).toBeUndefined()
    expect(parseYear(null)).toBeUndefined()
  })
})

describe('weekdayInTimezone', () => {
  it('reads the weekday in the schedule’s zone, not the browser’s', () => {
    // 23:00 UTC on a Wednesday is already Thursday in Tokyo.
    const at = new Date('2026-09-09T23:00:00Z')
    expect(weekdayInTimezone('UTC', at)).toBe(2)
    expect(weekdayInTimezone('Asia/Tokyo', at)).toBe(3)
  })

  it('falls back to the browser’s zone rather than blanking the highlight', () => {
    const at = new Date('2026-09-09T12:00:00Z')
    expect(weekdayInTimezone('Not/AZone', at)).toBe(2)
    expect(weekdayInTimezone('', at)).toBe(2)
  })

  it('highlights nothing when Intl answers with a weekday it does not know', () => {
    class UnknownWeekday {
      format(): string {
        return 'Sept'
      }
    }
    vi.stubGlobal('Intl', { DateTimeFormat: UnknownWeekday })

    expect(weekdayInTimezone('UTC', new Date('2026-09-09T12:00:00Z'))).toBe(-1)
  })
})

describe('isFollowing', () => {
  it('counts the statuses a person still intends to watch', () => {
    expect(isFollowing('watching')).toBe(true)
    expect(isFollowing('planned')).toBe(true)
    expect(isFollowing('on_hold')).toBe(true)
    expect(isFollowing('dropped')).toBe(false)
    expect(isFollowing('completed')).toBe(false)
    expect(isFollowing(null)).toBe(false)
  })
})

describe('useSchedule', () => {
  it('asks for the current season when no season is pinned', async () => {
    const fetchMock = mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })
    const client = createQueryClient()

    const { result } = renderHook(() => useSchedule(), { wrapper: wrapperFor(client) })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/schedule'])
    expect(client.getQueryData(scheduleQueryKey())).toEqual(SCHEDULE_PAGE)
  })

  it('sends year and season as query parameters when both are given', async () => {
    const fetchMock = mockApi({
      'GET /api/schedule?year=2026&season=SUMMER': { body: SCHEDULE_PAGE },
    })
    const client = createQueryClient()

    const { result } = renderHook(() => useSchedule(2026, 'SUMMER'), {
      wrapper: wrapperFor(client),
    })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/schedule?year=2026&season=SUMMER'])
    expect(client.getQueryData(scheduleQueryKey(2026, 'SUMMER'))).toEqual(SCHEDULE_PAGE)
  })
})

describe('useHome', () => {
  it('fetches the home aggregate', async () => {
    const fetchMock = mockApi({ 'GET /api/home': { body: HOME_PAGE } })
    const client = createQueryClient()

    const { result } = renderHook(() => useHome(), { wrapper: wrapperFor(client) })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })
    expect(requestsMade(fetchMock)).toEqual(['GET /api/home'])
    expect(result.current.data?.behind).toHaveLength(1)
  })
})

/**
 * Records every query key handed to `invalidateQueries` while still doing the
 * real invalidation, so a test can assert on both the call and its effect.
 */
function trackInvalidations(client: QueryClient): unknown[][] {
  const keys: unknown[][] = []
  const real = client.invalidateQueries.bind(client)

  vi.spyOn(client, 'invalidateQueries').mockImplementation((filters, options) => {
    const key = filters?.queryKey
    if (key !== undefined) keys.push([...key])
    return real(filters, options)
  })

  return keys
}

describe('list writes and the M4 caches', () => {
  it('invalidates the schedule and the home page after setting a status', async () => {
    mockApi({ [`PUT /api/list/${FRIEREN.id}`]: { body: listEntry({ status: 'watching' }) } })
    const client = createQueryClient()
    const invalidated = trackInvalidations(client)

    const { result } = renderHook(() => useSetListEntry(), { wrapper: wrapperFor(client) })
    act(() => {
      result.current.mutate({ animeId: FRIEREN.id, status: 'watching' })
    })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })

    expect(invalidated).toContainEqual(['schedule'])
    expect(invalidated).toContainEqual(['home'])
  })

  it('invalidates them again when the show comes off the list', async () => {
    mockApi({ [`DELETE /api/list/${FRIEREN.id}`]: { status: 204 } })
    const client = createQueryClient()
    const invalidated = trackInvalidations(client)

    const { result } = renderHook(() => useRemoveListEntry(), { wrapper: wrapperFor(client) })
    act(() => {
      result.current.mutate(FRIEREN.id)
    })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })

    expect(invalidated).toContainEqual(['schedule'])
    expect(invalidated).toContainEqual(['home'])
  })

  it('marks a cached schedule stale so the highlight refetches', async () => {
    mockApi({ [`PUT /api/list/${FRIEREN.id}`]: { body: listEntry({ status: 'watching' }) } })
    const client = createQueryClient()
    client.setQueryData(scheduleQueryKey(), SCHEDULE_PAGE)
    client.setQueryData(['home'], HOME_PAGE)

    const { result } = renderHook(() => useSetListEntry(), { wrapper: wrapperFor(client) })
    act(() => {
      result.current.mutate({ animeId: FRIEREN.id, status: 'watching' })
    })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })

    expect(client.getQueryState(scheduleQueryKey())?.isInvalidated).toBe(true)
    expect(client.getQueryState(['home'])?.isInvalidated).toBe(true)
  })
})

/**
 * The invalidations above are the correction; these are the bridge to it. A
 * refetch takes a round trip, and for that round trip the row a person just
 * changed has to already read the way they left it.
 */
describe('list writes patch the M4 caches before the refetch lands', () => {
  /** Runs one mutation to completion against a client seeded with both caches. */
  async function afterSetting(
    client: QueryClient,
    status: ListStatus,
    animeId = FRIEREN.id,
  ): Promise<void> {
    mockApi({ [`PUT /api/list/${animeId}`]: { body: listEntry({ anime_id: animeId, status }) } })
    const { result } = renderHook(() => useSetListEntry(), { wrapper: wrapperFor(client) })
    act(() => {
      result.current.mutate({ animeId, status })
    })
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })
  }

  function seeded(): QueryClient {
    const client = createQueryClient()
    client.setQueryData(scheduleQueryKey(), SCHEDULE_PAGE)
    client.setQueryData(homeQueryKey, HOME_PAGE)
    return client
  }

  function scheduleRow(client: QueryClient, weekday: number) {
    const page = client.getQueryData<SchedulePage>(scheduleQueryKey())
    return page?.days.find((day) => day.weekday === weekday)?.entries[0]
  }

  it('moves the schedule row’s status and drops its accent edge in one go', async () => {
    const client = seeded()

    await afterSetting(client, 'completed')

    const row = scheduleRow(client, 1)
    expect(row?.list_status).toBe('completed')
    expect(row?.anime.list_status).toBe('completed')
    // Completed is on the list but no longer followed (FR-C3).
    expect(row?.following).toBe(false)
  })

  it('patches an unscheduled row the same way', async () => {
    const client = seeded()

    await afterSetting(client, 'watching', FRIEREN_SPECIAL.id)

    const page = client.getQueryData<SchedulePage>(scheduleQueryKey())
    expect(page?.unscheduled[0]?.list_status).toBe('watching')
    expect(page?.unscheduled[0]?.following).toBe(true)
    // A show it does not name is untouched, object identity and all.
    expect(scheduleRow(client, 1)).toBe(SCHEDULE_PAGE.days[1]?.entries[0])
  })

  it('leaves a followed show on the home page with its new status', async () => {
    const client = seeded()

    await afterSetting(client, 'on_hold')

    const home = client.getQueryData<HomePage>(homeQueryKey)
    expect(home?.behind).toHaveLength(1)
    expect(home?.behind[0]?.entry.status).toBe('on_hold')
    expect(home?.behind[0]?.anime.list_status).toBe('on_hold')
    expect(home?.new_this_week[0]?.anime.list_status).toBe('on_hold')
  })

  it('takes a completed show out of "behind on" immediately', async () => {
    const client = seeded()

    await afterSetting(client, 'completed')

    const home = client.getQueryData<HomePage>(homeQueryKey)
    expect(home?.behind).toHaveLength(0)
    // It still aired this week; only the catch-up section is about following.
    expect(home?.new_this_week).toHaveLength(2)
    expect(home?.new_this_week[0]?.anime.list_status).toBe('completed')
  })

  it('clears the row and the card when the show comes off the list', async () => {
    mockApi({ [`DELETE /api/list/${FRIEREN.id}`]: { status: 204 } })
    const client = seeded()

    const { result } = renderHook(() => useRemoveListEntry(), { wrapper: wrapperFor(client) })
    act(() => {
      result.current.mutate(FRIEREN.id)
    })
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })

    const row = scheduleRow(client, 1)
    expect(row?.list_status).toBeNull()
    expect(row?.following).toBe(false)

    const home = client.getQueryData<HomePage>(homeQueryKey)
    expect(home?.behind).toHaveLength(0)
    expect(home?.new_this_week[0]?.anime.list_status).toBeNull()
  })
})
