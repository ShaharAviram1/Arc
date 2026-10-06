import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import type { DownloadRecord } from '@/offline/downloads'
import { memoryStore } from '@/offline/store'
import { PLAY_INFO } from '@/test/animeFixtures'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

/**
 * Trip copies on the device (M19 T6, FR-A12, FR-S9): kept straight from the
 * copy's URL, confirmed to the server when whole, confirmed again until the
 * server has heard, and released when taken off the device.
 */

const ALICE = 1
const TRIP = 77
const SMALL = 'episode-9001-o.mp4'
const COPY_URL = '/media/9001/offline.mp4'

async function alice(options: Parameters<typeof managerHarness>[0] = {}) {
  const harness = managerHarness(options)
  harness.manager.setOwner(ALICE)
  await harness.manager.hydrate()
  return harness
}

/** A promise the test settles by hand, for a confirmation in flight. */
function deferred() {
  let resolve: () => void = () => undefined
  let reject: (error: unknown) => void = () => undefined
  const promise = new Promise<void>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

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

describe('keepTripCopy', () => {
  it('downloads the trip copy straight from its URL, with no offline request', async () => {
    const requestCopy = vi.fn()
    const { manager, worker } = await alice({ requestCopy })

    await manager.keepTripCopy({ episodeId: 9001, tripId: TRIP, url: COPY_URL })

    expect(requestCopy).not.toHaveBeenCalled()
    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: SMALL, url: COPY_URL })
    expect(manager.record(9001)).toMatchObject({
      state: 'downloading',
      variant: 'small',
      tripId: TRIP,
      fullUrl: null,
    })
  })

  it('does nothing while nobody is signed in', async () => {
    const harness = managerHarness()
    await harness.manager.hydrate()
    await harness.manager.keepTripCopy({ episodeId: 9001, tripId: TRIP, url: COPY_URL })
    expect(harness.worker.commands).toEqual([])
  })
})

describe('confirming a trip copy', () => {
  it('confirms with the ETag on the worker’s done, then stops asking', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const { manager, worker } = await alice({ confirmDelivered })
    await manager.keepTripCopy({ episodeId: 9001, tripId: TRIP, url: COPY_URL })

    worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"', run: 1 })

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, '"o"')
    await vi.waitFor(() => {
      expect(manager.record(9001)?.confirm).toBe('done')
    })
    manager.confirmPending()
    manager.nudge()
    expect(confirmDelivered).toHaveBeenCalledTimes(1)
  })

  it('leaves a failed confirmation pending and sends it again on the next try', async () => {
    const first = deferred()
    const confirmDelivered = vi
      .fn<() => Promise<unknown>>()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue(null)
    const { manager, worker } = await alice({ confirmDelivered })
    await manager.keepTripCopy({ episodeId: 9001, tripId: TRIP, url: COPY_URL })
    worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"', run: 1 })

    // A tick while the first is still in flight sends nothing more.
    manager.confirmPending()
    expect(confirmDelivered).toHaveBeenCalledTimes(1)

    first.reject(new TypeError('Load failed'))
    await vi.waitFor(() => {
      expect(manager.record(9001)?.confirm).toBe('pending')
    })

    manager.nudge()
    expect(confirmDelivered).toHaveBeenCalledTimes(2)
    await vi.waitFor(() => {
      expect(manager.record(9001)?.confirm).toBe('done')
    })
  })

  it('retries a pending confirmation at launch', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const record = tripRecord({ confirm: 'pending' })
    const harness = managerHarness({ confirmDelivered, initial: [recordEntry(record)] })
    harness.files.set(SMALL, 210)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, '"o"')
    await vi.waitFor(() => {
      expect(harness.manager.record(9001)?.confirm).toBe('done')
    })
  })

  it('stops asking when the server says the trip is not this account’s', async () => {
    const confirmDelivered = vi.fn(() => Promise.reject(new ApiError(404, { detail: 'x' })))
    const harness = managerHarness({
      confirmDelivered,
      initial: [recordEntry(tripRecord({ confirm: 'pending' }))],
    })
    harness.files.set(SMALL, 210)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    await vi.waitFor(() => {
      expect(harness.manager.record(9001)?.confirm).toBe('done')
    })
    harness.manager.confirmPending()
    expect(confirmDelivered).toHaveBeenCalledTimes(1)
  })

  it('never confirms another account’s copy', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const harness = managerHarness({
      confirmDelivered,
      initial: [recordEntry(tripRecord({ userId: 2, confirm: 'pending' }))],
    })
    harness.files.set(SMALL, 210)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()
    harness.manager.confirmPending()
    expect(confirmDelivered).not.toHaveBeenCalled()
  })
})

describe('adoptTrip', () => {
  it('confirms a full-size copy already on the device at once', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const full = downloadedRecord(ALICE)
    const harness = managerHarness({ confirmDelivered, initial: [recordEntry(full)] })
    harness.files.set(full.name, full.bytes)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    harness.manager.adoptTrip(9001, TRIP, null)

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, '"e"')
    expect(harness.manager.record(9001)).toMatchObject({ tripId: TRIP, variant: 'full' })
    expect(harness.worker.commands).toEqual([])
  })

  it('leaves a download paused by hand exactly as it is', async () => {
    const paused = tripRecord({
      tripId: undefined,
      state: 'paused',
      reason: 'by-hand',
      message: 'Paused.',
      bytes: 50,
    })
    const harness = managerHarness({ initial: [recordEntry(paused)] })
    harness.files.set(SMALL, 50)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    harness.manager.adoptTrip(9001, TRIP, COPY_URL)

    expect(harness.manager.record(9001)).toMatchObject({ state: 'paused', reason: 'by-hand' })
    expect(harness.worker.commands).toEqual([])
  })

  it('takes the trip copy for a small copy that vanished from the server', async () => {
    const gone = tripRecord({ state: 'failed', reason: 'gone', bytes: 0, total: 0, url: null })
    const harness = managerHarness({ initial: [recordEntry(gone)] })
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    harness.manager.adoptTrip(9001, TRIP, COPY_URL)

    await vi.waitFor(() => {
      expect(harness.worker.lastCommand()).toMatchObject({ name: SMALL, url: COPY_URL })
    })
  })
})

describe('taking a trip copy off the device', () => {
  it('tells the server, and keeps the hook from fetching it again until asked', async () => {
    const releaseDelivered = vi.fn(() => Promise.resolve(null))
    const notes = memoryStore()
    const harness = managerHarness({
      releaseDelivered,
      notes,
      initial: [recordEntry(tripRecord({ confirm: 'done' }))],
    })
    harness.files.set(SMALL, 210)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    await harness.manager.remove(9001)

    expect(releaseDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001)
    expect(harness.manager.isDeclined(TRIP, 9001)).toBe(true)
    expect(await notes.get('trip-skip:1:77:9001')).toBe(true)

    harness.manager.forgetDecline(TRIP, 9001)
    expect(harness.manager.isDeclined(TRIP, 9001)).toBe(false)
    expect(await notes.get('trip-skip:1:77:9001')).toBeUndefined()
  })

  it('remembers the decline across a launch', async () => {
    const notes = memoryStore([['trip-skip:1:77:9001', true]])
    const { manager } = await alice({ notes })
    expect(manager.isDeclined(TRIP, 9001)).toBe(true)
    expect(manager.isDeclined(TRIP, 9002)).toBe(false)
  })

  it('makes no release call for a copy kept outside a trip', async () => {
    const releaseDelivered = vi.fn(() => Promise.resolve(null))
    const full = downloadedRecord(ALICE)
    const harness = managerHarness({ releaseDelivered, initial: [recordEntry(full)] })
    harness.files.set(full.name, full.bytes)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()
    await harness.manager.remove(9001)
    expect(releaseDelivered).not.toHaveBeenCalled()
  })
})

describe('a trip record waiting for its copy', () => {
  it('is not polled on the ready-only offline route, and a 404 there is not "gone"', async () => {
    const pollCopy = vi.fn(() => Promise.reject(new ApiError(404, { detail: 'x' })))
    const requestCopy = vi.fn(() => Promise.reject(new ApiError(404, { detail: 'x' })))
    const waiting = tripRecord({ state: 'preparing', url: null, bytes: 0, total: 0, etag: null })
    const harness = managerHarness({ pollCopy, requestCopy, initial: [recordEntry(waiting)] })
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()
    harness.manager.nudge()
    expect(pollCopy).not.toHaveBeenCalled()

    // A copy that vanished mid-download asks once, and the 404 leaves it waiting.
    harness.manager.adoptTrip(9001, TRIP, COPY_URL)
    await vi.waitFor(() => {
      expect(harness.worker.lastCommand()).toMatchObject({ url: COPY_URL })
    })
    harness.worker.emit({
      type: 'failed',
      name: SMALL,
      offset: 0,
      code: 'gone',
      reason: '404',
      run: 1,
    })
    await vi.waitFor(() => {
      expect(requestCopy).toHaveBeenCalledTimes(1)
    })
    await vi.waitFor(() => {
      expect(harness.manager.record(9001)).toMatchObject({ state: 'preparing', url: null })
    })
  })
})
