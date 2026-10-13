import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import {
  HOLDINGS_DEBOUNCE_MS,
  HOLDINGS_REFRESH_MS,
  holdingsKey,
  MAX_HOLDINGS,
  type DownloadRecord,
  type HoldingsNote,
} from '@/offline/downloads'
import { wireWatchedRemoval } from '@/offline/useDownloads'
import { reportOffline, reportOnline, resetNetwork } from '@/offline/network'
import type { FlushOutcome, Outbox } from '@/offline/outbox'
import { memoryStore, type KeyStore } from '@/offline/store'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import {
  downloadedRecord,
  managerHarness,
  recordEntry,
  TEST_DEVICE_ID,
} from '@/test/downloadFixtures'

/**
 * Device holdings (M20, FR-S9, architecture §5.4d): the device tells the
 * server which episodes it holds unwatched, at launch, on reconnect, when a
 * download completes and when one is deleted — debounced, deduped, queued
 * while offline, never for the demo account.
 */

const ALICE = 1
const START = Date.parse('2026-10-13T12:00:00Z')

interface Setup {
  initial?: Iterable<[string, unknown]>
  meta?: KeyStore
  online?: () => boolean
  put?: (deviceId: string, ids: number[]) => Promise<unknown>
  clock?: { now: number }
  demo?: boolean
}

async function launch(setup: Setup = {}) {
  const put = vi.fn(setup.put ?? (() => Promise.resolve(null)))
  const meta = setup.meta ?? memoryStore()
  const clock = setup.clock ?? { now: START }
  const harness = managerHarness({
    initial: setup.initial,
    putHoldings: put,
    meta,
    now: () => clock.now,
    isOnline: setup.online ?? (() => true),
  })
  // The bytes of every stored record are on "disk", so the launch keeps them.
  for (const [, record] of setup.initial ?? []) {
    const stored = record as DownloadRecord
    harness.files.set(stored.name, stored.bytes)
  }
  harness.manager.setOwner(ALICE, { demo: setup.demo === true })
  await harness.manager.hydrate()
  return { ...harness, put, meta, clock }
}

const held = (info = PLAY_INFO) => recordEntry(downloadedRecord(ALICE, info))

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  resetNetwork()
})

describe('reportHoldings', () => {
  it('reports what the device holds at launch, after the debounce', async () => {
    const { put, meta } = await launch({ initial: [held()] })

    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS - 1)
    expect(put).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)

    expect(put).toHaveBeenCalledTimes(1)
    expect(put).toHaveBeenCalledWith(TEST_DEVICE_ID, [9001])
    expect(await meta.get<HoldingsNote>(holdingsKey(ALICE))).toEqual({
      ids: [9001],
      sentAt: START,
    })
  })

  it('sends nothing at a first launch that holds nothing', async () => {
    const { put } = await launch()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS * 2)
    expect(put).not.toHaveBeenCalled()
  })

  it('leaves out a copy marked watched, and one still downloading', async () => {
    const watched = { ...downloadedRecord(ALICE, PLAY_INFO_EPISODE_2), watched: true }
    const partial = { ...downloadedRecord(ALICE), state: 'paused' as const, bytes: 10 }
    const { put } = await launch({ initial: [recordEntry(watched), recordEntry(partial)] })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    // Nothing complete and unwatched, and nothing ever said: no request.
    expect(put).not.toHaveBeenCalled()
  })

  it('a completed download triggers exactly one PUT with that id', async () => {
    const { manager, worker, put } = await launch()
    await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })

    worker.emit({ type: 'done', name: 'episode-9001.mp4', offset: 10, total: 10, etag: '"e"' })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS * 3)

    expect(put).toHaveBeenCalledTimes(1)
    expect(put).toHaveBeenCalledWith(TEST_DEVICE_ID, [9001])
  })

  it('debounces a burst of triggers into one request', async () => {
    const { manager, put } = await launch({ initial: [held()] })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    put.mockClear()
    await manager.remove(9001)

    manager.reportHoldings()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS - 1000)
    manager.reportHoldings()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS - 1000)
    expect(put).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1000)

    expect(put).toHaveBeenCalledTimes(1)
  })

  it('deleting a copy sends the set without it', async () => {
    const { manager, put } = await launch({ initial: [held(), held(PLAY_INFO_EPISODE_2)] })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).toHaveBeenLastCalledWith(TEST_DEVICE_ID, [9001, 9002])

    await manager.remove(9001)
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)

    expect(put).toHaveBeenCalledTimes(2)
    expect(put).toHaveBeenLastCalledWith(TEST_DEVICE_ID, [9002])
  })

  it('deleting the last copy sends an empty set', async () => {
    const { manager, put } = await launch({ initial: [held()] })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    await manager.remove(9001)
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).toHaveBeenLastCalledWith(TEST_DEVICE_ID, [])
  })

  it('a second launch with an unchanged set sends nothing within 10 minutes, then refreshes', async () => {
    const meta = memoryStore()
    const clock = { now: START }
    const first = await launch({ initial: [held()], meta, clock })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(first.put).toHaveBeenCalledTimes(1)

    clock.now = START + HOLDINGS_REFRESH_MS - 60_000
    const second = await launch({ initial: [held()], meta, clock })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(second.put).not.toHaveBeenCalled()

    clock.now = START + HOLDINGS_REFRESH_MS + 1
    const third = await launch({ initial: [held()], meta, clock })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(third.put).toHaveBeenCalledTimes(1)
  })

  it('a changed set is sent at once, however recent the last report', async () => {
    const meta = memoryStore([[holdingsKey(ALICE), { ids: [9002], sentAt: START }]])
    const { put } = await launch({ initial: [held()], meta })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).toHaveBeenCalledWith(TEST_DEVICE_ID, [9001])
  })

  it('queues while offline and sends on the outbox’s next landed flush', async () => {
    let online = false
    const { manager, put, meta } = await launch({ initial: [held()], online: () => online })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).not.toHaveBeenCalled()
    expect(await meta.get<HoldingsNote>(holdingsKey(ALICE))).toMatchObject({ pending: true })

    online = true
    // An outbox whose flush reached the server: it tells its listeners.
    const flushed = new Set<(outcome: FlushOutcome) => void>()
    const box = {
      onWatched: () => () => undefined,
      onFlushed: (listener: (outcome: FlushOutcome) => void) => {
        flushed.add(listener)
        return () => flushed.delete(listener)
      },
    } as unknown as Outbox
    const unwire = wireWatchedRemoval(box, manager)
    for (const listener of flushed) listener({ ok: true, sent: 1, removed: 1, rejected: 0 })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    unwire()

    expect(put).toHaveBeenCalledTimes(1)
    expect(put).toHaveBeenCalledWith(TEST_DEVICE_ID, [9001])
    expect(await meta.get<HoldingsNote>(holdingsKey(ALICE))).toEqual({
      ids: [9001],
      sentAt: START,
    })
  })

  it('a request that gets no answer waits for the reconnect', async () => {
    const put = vi.fn<(deviceId: string, ids: number[]) => Promise<unknown>>()
    put.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    put.mockResolvedValue(null)
    const { manager, meta } = await launch({ initial: [held()], put })
    const unlisten = manager.listen()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(await meta.get<HoldingsNote>(holdingsKey(ALICE))).toMatchObject({ pending: true })

    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    unlisten()

    expect(put).toHaveBeenCalledTimes(2)
    expect(await meta.get<HoldingsNote>(holdingsKey(ALICE))).toEqual({
      ids: [9001],
      sentAt: START,
    })
  })

  it('a launch report the server fails (5xx) waits for the reconnect', async () => {
    const put = vi.fn<(deviceId: string, ids: number[]) => Promise<unknown>>()
    put.mockRejectedValueOnce(new ApiError(500, { detail: 'boom' }))
    put.mockResolvedValue(null)
    const { manager, meta } = await launch({ initial: [held()], put })
    const unlisten = manager.listen()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(await meta.get<HoldingsNote>(holdingsKey(ALICE))).toMatchObject({ pending: true })

    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    unlisten()

    expect(put).toHaveBeenCalledTimes(2)
    expect(put).toHaveBeenLastCalledWith(TEST_DEVICE_ID, [9001])
    expect(await meta.get<HoldingsNote>(holdingsKey(ALICE))).toEqual({
      ids: [9001],
      sentAt: START,
    })
  })

  it('over the 500 cap, keeps the highest (newest) episode ids', async () => {
    const many = Array.from({ length: MAX_HOLDINGS + 20 }, (_, index) => {
      const episodeId = 20_000 + index
      const record = downloadedRecord(ALICE)
      return recordEntry({ ...record, episodeId, name: `episode-${String(episodeId)}.mp4` })
    })
    const { put } = await launch({ initial: many })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)

    const sent = put.mock.calls[0]?.[1] ?? []
    expect(sent).toHaveLength(MAX_HOLDINGS)
    expect(sent[0]).toBe(20_020)
    expect(sent.at(-1)).toBe(20_000 + MAX_HOLDINGS + 19)
  })

  it('reports on reconnect when Arc sees its server answer again', async () => {
    const { manager, put, meta } = await launch({ initial: [held()] })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).toHaveBeenCalledTimes(1)
    // A note old enough that the reconnect report is not deduped away.
    await meta.put(holdingsKey(ALICE), { ids: [9001], sentAt: START, pending: true })
    const unlisten = manager.listen()

    reportOffline()
    reportOnline()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    unlisten()

    expect(put).toHaveBeenCalledTimes(2)
  })

  it('never reports for the demo account', async () => {
    const { manager, put } = await launch({ initial: [held()], demo: true })
    manager.reportHoldings()
    await manager.sendHoldings()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS * 2)
    expect(put).not.toHaveBeenCalled()
  })

  it('stops after the server refuses with a 403', async () => {
    const put = vi.fn(() => Promise.reject(new ApiError(403, { detail: 'demo_account' })))
    const { manager } = await launch({ initial: [held()], put })
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).toHaveBeenCalledTimes(1)

    await manager.remove(9001)
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).toHaveBeenCalledTimes(1)
  })

  it('reports nothing while nobody is signed in', async () => {
    const put = vi.fn(() => Promise.resolve(null))
    const harness = managerHarness({ putHoldings: put, initial: [held()] })
    await harness.manager.hydrate()
    harness.manager.reportHoldings()
    await vi.advanceTimersByTimeAsync(HOLDINGS_DEBOUNCE_MS)
    expect(put).not.toHaveBeenCalled()
  })
})
