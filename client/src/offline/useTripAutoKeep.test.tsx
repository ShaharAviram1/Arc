import { QueryClientProvider } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { User } from '@/lib/auth'
import { createQueryClient } from '@/lib/queryClient'
import type { Trip, TripEpisode } from '@/lib/trips'
import { setDownloads, type DownloadRecord } from '@/offline/downloads'
import { autoKeep, keepsTrips, useTripAutoKeep } from '@/offline/useTripAutoKeep'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { mockApi, requestsMade, TEST_DEMO_USER, TEST_USER } from '@/test/apiMock'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

/**
 * The auto-keep hook (M19 T6, FR-A12): what one pass over the current trip
 * does, and when the hook runs at all.
 */

const USER = TEST_USER.id
const TRIP = 77

function tripEpisode(id: number, number: number, phase: TripEpisode['phase']): TripEpisode {
  return {
    episode_id: id,
    number,
    phase,
    progress: phase === 'available' ? 1 : null,
    size: phase === 'available' ? 98_000_000 : null,
    delivered: phase === 'delivered',
    url: phase === 'available' || phase === 'delivered' ? `/media/${String(id)}/offline.mp4` : null,
  }
}

function trip(episodes: TripEpisode[], state: Trip['state'] = 'active'): Trip {
  return {
    id: TRIP,
    anime_id: PLAY_INFO.anime.id,
    anime_title: PLAY_INFO.anime.title.preferred,
    first_number: 1,
    last_number: 2,
    count: episodes.length,
    state,
    created_at: '2026-10-06T08:00:00Z',
    deadline_at: '2026-10-20T08:00:00Z',
    episodes,
  }
}

async function managerWith(records: DownloadRecord[] = [], options = {}) {
  const harness = managerHarness({ ...options, initial: records.map(recordEntry) })
  for (const record of records) harness.files.set(record.name, record.bytes)
  harness.manager.setOwner(USER)
  await harness.manager.hydrate()
  return harness
}

function pretendOpfs() {
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: { getDirectory: () => Promise.reject(new Error('not in tests')) },
  })
  vi.stubGlobal('Worker', class {})
}

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'storage')
  setDownloads(null)
})

describe('autoKeep', () => {
  it('keeps each available episode the device has no record of, and no other', async () => {
    const { manager, worker } = await managerWith()

    await autoKeep(
      trip([tripEpisode(9001, 1, 'available'), tripEpisode(9002, 2, 'preparing')]),
      manager,
    )

    expect(manager.record(9001)).toMatchObject({ tripId: TRIP, variant: 'small' })
    expect(manager.record(9002)).toBeUndefined()
    expect(worker.commands).toHaveLength(1)
    expect(worker.lastCommand()).toMatchObject({ url: '/media/9001/offline.mp4' })
  })

  it('downloads nothing the server has given no URL for yet', async () => {
    const { manager, worker } = await managerWith()
    await autoKeep(trip([{ ...tripEpisode(9001, 1, 'available'), url: null }]), manager)
    expect(manager.record(9001)).toBeUndefined()
    expect(worker.commands).toEqual([])
  })

  it('never restarts a download the viewer paused by hand', async () => {
    const paused: DownloadRecord = {
      ...downloadedRecord(USER),
      state: 'paused',
      reason: 'by-hand',
      message: 'Paused.',
      total: 0,
      bytes: 10,
    }
    const { manager, worker } = await managerWith([paused])

    await autoKeep(trip([tripEpisode(9001, 1, 'available')]), manager)

    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'by-hand' })
    expect(worker.commands).toEqual([])
  })

  it('confirms an episode already on the device as a full-size copy at once', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const { manager, worker } = await managerWith([downloadedRecord(USER)], { confirmDelivered })

    await autoKeep(trip([tripEpisode(9001, 1, 'preparing')]), manager)

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, '"e"')
    expect(worker.commands).toEqual([])
  })

  it('leaves an episode taken off this device by hand until "Ask again"', async () => {
    const { manager, worker } = await managerWith()
    await manager.keepTripCopy({ episodeId: 9001, tripId: TRIP, url: '/media/9001/offline.mp4' })
    await manager.remove(9001)
    const before = worker.commands.length

    await autoKeep(trip([tripEpisode(9001, 1, 'available')]), manager)
    expect(manager.record(9001)).toBeUndefined()
    expect(worker.commands).toHaveLength(before)

    manager.forgetDecline(TRIP, 9001)
    await autoKeep(trip([tripEpisode(9001, 1, 'available')]), manager)
    expect(manager.record(9001)?.tripId).toBe(TRIP)
  })

  it('sends a pending confirmation again on every pass, trip or none', async () => {
    const confirmDelivered = vi
      .fn<() => Promise<unknown>>()
      .mockRejectedValueOnce(new TypeError('Load failed'))
      .mockResolvedValue(null)
    const pending: DownloadRecord = {
      ...downloadedRecord(USER, PLAY_INFO_EPISODE_2),
      tripId: TRIP,
      confirm: 'pending',
    }
    const { manager } = await managerWith([pending], { confirmDelivered })
    // The launch-time try failed.
    await vi.waitFor(() => {
      expect(confirmDelivered).toHaveBeenCalledTimes(1)
    })
    await Promise.resolve()

    await autoKeep(null, manager)

    expect(confirmDelivered).toHaveBeenCalledTimes(2)
    await vi.waitFor(() => {
      expect(manager.record(9002)?.confirm).toBe('done')
    })
  })

  it('does nothing for a trip that has ended, or with nobody signed in', async () => {
    const { manager, worker } = await managerWith()
    await autoKeep(trip([tripEpisode(9001, 1, 'available')], 'cancelled'), manager)
    expect(worker.commands).toEqual([])

    manager.setOwner(null)
    await autoKeep(trip([tripEpisode(9001, 1, 'available')]), manager)
    expect(worker.commands).toEqual([])
  })
})

describe('useTripAutoKeep', () => {
  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>
  }

  it('is off for the demo account, for nobody, and where the browser cannot keep files', () => {
    expect(keepsTrips(TEST_USER)).toBe(false) // jsdom: no OPFS
    pretendOpfs()
    expect(keepsTrips(TEST_USER)).toBe(true)
    expect(keepsTrips(TEST_DEMO_USER)).toBe(false)
    expect(keepsTrips(null)).toBe(false)
    expect(keepsTrips(undefined)).toBe(false)
  })

  it('asks for the current trip and keeps what is available', async () => {
    pretendOpfs()
    const harness = managerHarness()
    harness.manager.setOwner(USER)
    setDownloads(harness.manager)
    const fetchMock = mockApi({
      'GET /api/trips/current': { body: trip([tripEpisode(9001, 1, 'available')]) },
    })

    renderHook(
      () => {
        useTripAutoKeep(TEST_USER)
      },
      { wrapper },
    )

    await vi.waitFor(() => {
      expect(harness.manager.record(9001)?.tripId).toBe(TRIP)
    })
    expect(requestsMade(fetchMock)).toContain('GET /api/trips/current')
  })

  it('asks nothing for the demo account or once signed out', async () => {
    pretendOpfs()
    const fetchMock = mockApi({ 'GET /api/trips/current': { body: null } })

    const initialProps: { me: User | null } = { me: TEST_DEMO_USER }
    const { rerender } = renderHook(
      ({ me }: { me: User | null }) => {
        useTripAutoKeep(me)
      },
      { wrapper, initialProps },
    )
    rerender({ me: null })
    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(fetchMock).not.toHaveBeenCalled()
  })
})
