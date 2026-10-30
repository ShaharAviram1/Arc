import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { MESSAGES, type DownloadRecord } from '@/offline/downloads'
import type { SizeRead } from '@/offline/opfs'
import { PLAY_INFO } from '@/test/animeFixtures'
import {
  downloadedRecord,
  managerHarness,
  recordEntry,
  TEST_DEVICE_ID,
} from '@/test/downloadFixtures'
import { jsonBodyOf, mockApi } from '@/test/apiMock'

/**
 * A held copy is never destroyed, and "delivered" means verified (M20.4 E,
 * owner 2026-10-30): the launch reconcile, Try again, a media error, a stuck
 * trip record, the SHA-256 checks and the event log.
 */

const ALICE = 1
const TRIP = 77
const FULL = 'episode-9001.mp4'
const SMALL = 'episode-9001-o.mp4'
const COPY_URL = '/media/9001/offline.mp4'
const SHA_A = 'a'.repeat(64)
const SHA_B = 'b'.repeat(64)

function tripRecord(patch: Partial<DownloadRecord> = {}): DownloadRecord {
  return {
    ...downloadedRecord(ALICE, PLAY_INFO, 210),
    name: SMALL,
    url: COPY_URL,
    variant: 'small',
    tripId: TRIP,
    etag: '"o"',
    ...patch,
  }
}

/** A launch with these records and the files on disk the test gives. */
async function launch(
  records: DownloadRecord[],
  files: Record<string, number> = {},
  options: Parameters<typeof managerHarness>[0] = {},
) {
  const harness = managerHarness({ ...options, initial: records.map(recordEntry) })
  for (const [name, size] of Object.entries(files)) harness.files.set(name, size)
  harness.manager.setOwner(ALICE)
  await harness.manager.hydrate()
  return harness
}

/** A size reader that answers from a script, then from the files map. */
function scripted(script: SizeRead[], fallback: SizeRead) {
  const queue = [...script]
  return vi.fn((): Promise<SizeRead> => Promise.resolve(queue.shift() ?? fallback))
}

async function kinds(manager: { events: { entries(): Promise<{ kind: string }[]> } }) {
  return (await manager.events.entries()).map((event) => event.kind)
}

describe('the launch reconcile (M20.4 E)', () => {
  it('reads a short or missing copy again before deciding, and keeps one that is there', async () => {
    const sizeOf = scripted(['missing', 1000], 1000)
    const { manager, removed } = await launch([downloadedRecord(ALICE)], {}, { sizeOf })

    expect(sizeOf).toHaveBeenCalledTimes(2)
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', bytes: 1000 })
    expect(removed).toEqual([])
    expect(await kinds(manager)).toContain('reconcile')
  })

  it('marks evicted only a file that is missing on both reads', async () => {
    const { manager, reads } = await launch([downloadedRecord(ALICE)])

    // Its own slot twice, then the other slots for a whole copy no record names.
    expect(reads).toEqual([FULL, FULL, 'episode-9001-b.mp4', 'episode-9001-c.mp4'])
    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'evicted' })
    const events = await manager.events.entries()
    expect(events.some((event) => event.detail?.includes('evicted') === true)).toBe(true)
  })

  it('leaves a record exactly as it was when the storage cannot be read', async () => {
    const harness = managerHarness({ initial: [recordEntry(downloadedRecord(ALICE))] })
    harness.unreadable.add(FULL)
    harness.files.set(FULL, 1000)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    expect(harness.manager.record(9001)).toMatchObject({
      state: 'downloaded',
      bytes: 1000,
      reason: null,
    })
    expect(harness.manager.record(9001)?.fresh).toBeUndefined()
    expect(harness.removed).toEqual([])
    expect(await harness.store.get(`${String(ALICE)}:9001`)).toMatchObject({ state: 'downloaded' })
  })

  it('pauses a download that was running, keeping its bytes, when the read fails', async () => {
    const running = { ...downloadedRecord(ALICE), state: 'downloading' as const, bytes: 400 }
    const harness = managerHarness({ initial: [recordEntry(running)] })
    harness.unreadable.add(FULL)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    expect(harness.manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'interrupted',
      bytes: 400,
    })
  })

  it('takes a copy short on both reads for a download to finish, not one to start over', async () => {
    const { manager } = await launch([downloadedRecord(ALICE)], { [FULL]: 600 })

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'size', bytes: 600 })
    expect(manager.record(9001)?.fresh).toBeUndefined()
  })
})

describe('Try again on a held copy (M20.4 E)', () => {
  it('puts a copy wrongly taken for evicted back when the server answers 404', async () => {
    const evicted = {
      ...downloadedRecord(ALICE),
      state: 'failed' as const,
      reason: 'evicted' as const,
      message: MESSAGES.evicted,
      fresh: true,
    }
    const requestCopy = vi.fn()
    const { manager, worker, removed, files } = await launch([evicted], {}, { requestCopy })
    // The file turns out to be there after all.
    files.set(FULL, 1000)

    manager.resume(9001)
    expect(worker.lastCommand()).toMatchObject({ name: FULL, fresh: true })
    worker.emit({
      type: 'failed',
      name: FULL,
      offset: 1000,
      code: 'gone',
      reason: 'the server no longer has this episode',
      intact: true,
      run: 1,
    })

    expect(manager.record(9001)).toMatchObject({
      state: 'downloaded',
      bytes: 1000,
      total: 1000,
      etag: '"e"',
      fresh: false,
    })
    expect(removed).toEqual([])
    expect(requestCopy).not.toHaveBeenCalled()
    expect(await kinds(manager)).toContain('kept')
  })

  it('keeps an unreadable small copy as it was, and does not ask the server again', async () => {
    const unreadable = tripRecord({
      state: 'failed',
      reason: 'unreadable',
      message: MESSAGES.unreadable,
      fresh: true,
    })
    const requestCopy = vi.fn()
    // At launch a whole file would make it downloaded again: it reads
    // missing then, and is back by the time Try again runs.
    const { manager, worker, removed, files } = await launch([unreadable], {}, { requestCopy })
    files.set(SMALL, 210)

    manager.resume(9001)
    worker.emit({
      type: 'failed',
      name: SMALL,
      offset: 210,
      code: 'gone',
      reason: 'the server no longer has this episode',
      intact: true,
      run: 1,
    })

    expect(manager.record(9001)).toMatchObject({
      state: 'failed',
      reason: 'unreadable',
      url: COPY_URL,
      bytes: 210,
    })
    expect(requestCopy).not.toHaveBeenCalled()
    expect(removed).toEqual([])
  })

  it('treats a 404 after the file was cut as gone, as before', async () => {
    const evicted = {
      ...downloadedRecord(ALICE),
      state: 'failed' as const,
      reason: 'evicted' as const,
      fresh: true,
    }
    const { manager, worker } = await launch([evicted])

    manager.resume(9001)
    worker.emit({ type: 'truncated', name: FULL, from: 1000, run: 1 })
    worker.emit({ type: 'failed', name: FULL, offset: 0, code: 'gone', reason: '404', run: 1 })

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'gone' })
    expect(await kinds(manager)).toContain('truncated')
  })

  it('starts over after a re-encode restart, since the file was not cut', async () => {
    const { manager, worker } = await launch([])
    await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })
    worker.emit({ type: 'progress', name: FULL, offset: 10, total: 100, etag: '"e"', run: 1 })
    worker.emit({ type: 'restarted', name: FULL, reason: 'etag', run: 1 })

    expect(manager.record(9001)).toMatchObject({ fresh: true, bytes: 0, etag: null })
  })
})

describe('a media error on a downloaded copy (M20.4 E)', () => {
  it('plays a file that checks out once more, and marks it only on a second error', async () => {
    const { manager, removed, revokes } = await launch([downloadedRecord(ALICE)], {
      [FULL]: 1000,
    })
    await manager.playableUrl(9001)
    const revoked = revokes.count

    expect(await manager.playbackFailed(9001)).toBe(`blob:${FULL}`)
    // The old URL is not revoked before a new one exists (minting does that).
    expect(revokes.count).toBe(revoked)
    expect(manager.record(9001)?.state).toBe('downloaded')

    expect(await manager.playbackFailed(9001)).toBeNull()
    expect(revokes.count).toBeGreaterThan(revoked)
    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'unreadable' })
    // Nothing on disk is touched.
    expect(removed).toEqual([])
  })

  it('counts afresh each time the player opens the episode', async () => {
    const { manager } = await launch([downloadedRecord(ALICE)], { [FULL]: 1000 })

    expect(await manager.playbackFailed(9001)).not.toBeNull()
    manager.enterPlayer(9001)()
    expect(await manager.playbackFailed(9001)).not.toBeNull()
    expect(manager.record(9001)?.state).toBe('downloaded')
  })

  it('marks a copy whose SHA-256 is not the one it arrived with at once', async () => {
    const record = { ...downloadedRecord(ALICE), sha256: SHA_A }
    const harness = await launch([record], { [FULL]: 1000 })
    harness.hashes.set(FULL, SHA_B)

    expect(await harness.manager.playbackFailed(9001)).toBeNull()
    expect(harness.manager.record(9001)).toMatchObject({ state: 'failed', reason: 'unreadable' })
  })

  it('says evicted for a file that is gone, and size for one that is short', async () => {
    const gone = await launch([downloadedRecord(ALICE)], { [FULL]: 1000 })
    gone.files.delete(FULL)
    expect(await gone.manager.playbackFailed(9001)).toBeNull()
    expect(gone.manager.record(9001)).toMatchObject({ state: 'failed', reason: 'evicted' })

    const short = await launch([downloadedRecord(ALICE)], { [FULL]: 1000 })
    short.files.set(FULL, 400)
    expect(await short.manager.playbackFailed(9001)).toBeNull()
    expect(short.manager.record(9001)).toMatchObject({ state: 'failed', reason: 'size' })
  })

  it('never marks a file it could not read for the check', async () => {
    const { manager, unreadable } = await launch([downloadedRecord(ALICE)], { [FULL]: 1000 })
    unreadable.add(FULL)

    expect(await manager.playbackFailed(9001)).toBe(`blob:${FULL}`)
    expect(await manager.playbackFailed(9001)).toBeNull()
    expect(manager.record(9001)?.state).toBe('downloaded')
  })

  it('never marks a file whose digest could not be computed', async () => {
    const record = { ...downloadedRecord(ALICE), sha256: SHA_A }
    const { manager } = await launch([record], { [FULL]: 1000 })

    await manager.playbackFailed(9001)
    expect(await manager.playbackFailed(9001)).toBeNull()
    expect(manager.record(9001)?.state).toBe('downloaded')
  })

  it('hashes through the worker when nothing else is given', async () => {
    const record = { ...downloadedRecord(ALICE), sha256: SHA_A }
    const { manager, worker } = await launch([record], { [FULL]: 1000 }, { hashOf: undefined })

    const answer = manager.playbackFailed(9001)
    await vi.waitFor(() => {
      expect(worker.lastCommand()).toEqual({ cmd: 'hash', name: FULL, ask: 1 })
    })
    worker.emit({ type: 'hashed', name: FULL, ask: 1, size: 1000, sha256: SHA_A })

    expect(await answer).toBe(`blob:${FULL}`)
  })
})

describe('a trip record left waiting on a delivered row (M20.4 E)', () => {
  const waiting = () =>
    tripRecord({
      state: 'preparing',
      url: null,
      bytes: 0,
      total: 0,
      etag: null,
      confirm: undefined,
    })

  it('asks the server again once, by itself, and logs it', async () => {
    const askTripAgain = vi.fn(() => Promise.resolve({}))
    const { manager } = await launch([waiting()], {}, { askTripAgain })

    manager.adoptTrip(9001, TRIP, null, null, { phase: 'delivered' })
    await vi.waitFor(() => {
      expect(manager.record(9001)?.tripReasked).toBe(true)
    })
    manager.adoptTrip(9001, TRIP, null, null, { phase: 'delivered' })

    expect(askTripAgain).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, TEST_DEVICE_ID)
    expect(await kinds(manager)).toContain('reask')
  })

  it('asks again on a later pass when the first found no network', async () => {
    const askTripAgain = vi
      .fn<() => Promise<unknown>>()
      .mockRejectedValueOnce(new TypeError('Load failed'))
      .mockResolvedValue({})
    const { manager } = await launch([waiting()], {}, { askTripAgain })

    manager.adoptTrip(9001, TRIP, null, null, { phase: 'delivered' })
    await vi.waitFor(() => {
      expect(askTripAgain).toHaveBeenCalledTimes(1)
    })
    await Promise.resolve()
    manager.adoptTrip(9001, TRIP, null, null, { phase: 'delivered' })
    await vi.waitFor(() => {
      expect(askTripAgain).toHaveBeenCalledTimes(2)
    })
  })

  it('then takes the copy when the trip offers it again', async () => {
    const { manager, worker } = await launch(
      [waiting()],
      {},
      {
        askTripAgain: () => Promise.resolve({}),
      },
    )
    manager.adoptTrip(9001, TRIP, null, null, { phase: 'delivered' })
    await vi.waitFor(() => {
      expect(manager.record(9001)?.tripReasked).toBe(true)
    })

    manager.adoptTrip(9001, TRIP, COPY_URL, '"o"', { phase: 'available', sha256: SHA_A })

    await vi.waitFor(() => {
      expect(worker.lastCommand()).toMatchObject({ name: SMALL, url: COPY_URL })
    })
    expect(manager.record(9001)).toMatchObject({ expected: SHA_A })
  })
})

describe('verified delivery (M20.4 E)', () => {
  async function keeping(options: Parameters<typeof managerHarness>[0] = {}) {
    const harness = await launch([], {}, options)
    await harness.manager.keepTripCopy({
      episodeId: 9001,
      tripId: TRIP,
      url: COPY_URL,
      sha256: SHA_A,
    })
    return harness
  }

  it('confirms with the digest and size the worker computed', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const { manager, worker } = await keeping({ confirmDelivered })

    worker.emit({
      type: 'done',
      name: SMALL,
      offset: 210,
      total: 210,
      etag: '"o"',
      sha256: SHA_A,
      run: 1,
    })

    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', sha256: SHA_A })
    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, '"o"', TEST_DEVICE_ID, {
      sha256: SHA_A,
      size: 210,
      variant: 'small',
    })
  })

  it('fetches a copy that differs from the server’s again once, then fails it', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const { manager, worker, files, removed } = await keeping({ confirmDelivered })
    files.set(SMALL, 210)
    const done = (run: number) => {
      worker.emit({
        type: 'done',
        name: SMALL,
        offset: 210,
        total: 210,
        etag: '"o"',
        sha256: SHA_B,
        run,
      })
    }

    done(1)
    await vi.waitFor(() => {
      expect(worker.commands).toHaveLength(2)
    })
    expect(worker.lastCommand()).toMatchObject({ name: SMALL, fresh: true, etag: null })
    // The device's own finding drops nothing: the fresh run cuts it later.
    expect(removed).toEqual([])
    expect(manager.record(9001)).toMatchObject({ state: 'downloading', rechecked: true })

    done(2)
    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'mismatch' })
    })
    expect(confirmDelivered).not.toHaveBeenCalled()
    expect(removed).toEqual([])
    expect(files.get(SMALL)).toBe(210)
    expect(await kinds(manager)).toContain('mismatch')
  })

  it('drops and fetches again a copy the server refuses as not its own (409 copy_mismatch)', async () => {
    const confirmDelivered = vi
      .fn<() => Promise<unknown>>()
      .mockRejectedValueOnce(new ApiError(409, { detail: 'copy_mismatch' }))
      .mockResolvedValue(null)
    const { manager, worker, files, removed } = await keeping({ confirmDelivered })
    files.set(SMALL, 210)

    worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"', run: 1 })

    await vi.waitFor(() => {
      expect(worker.commands).toHaveLength(2)
    })
    expect(removed).toEqual([SMALL])
    expect(worker.lastCommand()).toMatchObject({ name: SMALL, fresh: true })

    worker.emit({
      type: 'done',
      name: SMALL,
      offset: 210,
      total: 210,
      etag: '"o"',
      sha256: SHA_A,
      run: 2,
    })
    await vi.waitFor(() => {
      expect(manager.record(9001)?.confirm).toBe('done')
    })
  })

  it('fails a copy the server refuses twice, and drops it', async () => {
    const confirmDelivered = vi.fn(() =>
      Promise.reject(new ApiError(409, { detail: 'copy_mismatch' })),
    )
    const { manager, worker, files } = await keeping({ confirmDelivered })
    files.set(SMALL, 210)

    worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"', run: 1 })
    await vi.waitFor(() => {
      expect(worker.commands).toHaveLength(2)
    })
    files.set(SMALL, 210)
    worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"', run: 2 })

    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'mismatch' })
    })
    expect(files.has(SMALL)).toBe(false)
  })

  it('names the copy in the confirmation body', async () => {
    const fetchMock = mockApi({
      [`POST /api/trips/${String(TRIP)}/episodes/9001/delivered`]: { status: 204 },
    })
    const full = downloadedRecord(ALICE)
    const { manager } = await launch([full], { [FULL]: 1000 }, { confirmDelivered: undefined })

    manager.adoptTrip(9001, TRIP, COPY_URL, null)

    await vi.waitFor(() => {
      expect(manager.record(9001)?.confirm).toBe('done')
    })
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(jsonBodyOf(call?.[1])).toEqual({
      etag: '"e"',
      device_id: TEST_DEVICE_ID,
      sha256: null,
      size: 1000,
      variant: 'full',
    })
  })

  it('never drops a full-size copy over a copy_mismatch, and stops asking', async () => {
    const confirmDelivered = vi.fn(() =>
      Promise.reject(new ApiError(409, { detail: 'copy_mismatch' })),
    )
    const full = downloadedRecord(ALICE)
    const { manager, removed } = await launch([full], { [FULL]: 1000 }, { confirmDelivered })

    manager.adoptTrip(9001, TRIP, COPY_URL, null)

    await vi.waitFor(() => {
      expect(manager.record(9001)?.confirm).toBe('done')
    })
    expect(removed).toEqual([])
    expect(manager.record(9001)?.state).toBe('downloaded')
  })
})

describe('the event log (M20.4 E)', () => {
  it('records state changes with reasons, and sends them all on request', async () => {
    const postReport = vi.fn(() => Promise.resolve(null))
    const { manager, worker } = await launch([], {}, { postReport })
    await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })
    worker.emit({ type: 'failed', name: FULL, offset: 0, code: 'gone', reason: '404', run: 1 })

    expect(await manager.sendReport()).toBeGreaterThan(0)

    const [device, events] = postReport.mock.calls[0] as unknown as [
      string,
      { kind: string; episode_id?: number; detail?: string }[],
    ]
    expect(device).toBe(TEST_DEVICE_ID)
    expect(events).toContainEqual(
      expect.objectContaining({
        kind: 'state',
        episode_id: 9001,
        detail: 'downloading → failed (gone)',
      }),
    )
    expect(events).toContainEqual(expect.objectContaining({ kind: 'failed', episode_id: 9001 }))
  })

  it('rejects when the report cannot be sent, so the page can say so', async () => {
    const { manager } = await launch(
      [],
      {},
      {
        postReport: () => Promise.reject(new TypeError('Load failed')),
      },
    )
    await expect(manager.sendReport()).rejects.toThrow()
  })
})
