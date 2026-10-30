import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import type { DownloadRecord } from '@/offline/downloads'
import { memoryStore } from '@/offline/store'
import { PLAY_INFO } from '@/test/animeFixtures'
import {
  downloadedRecord,
  managerHarness,
  recordEntry,
  TEST_DEVICE_ID,
} from '@/test/downloadFixtures'

/**
 * Trip copies on the device (M19 T6, FR-A12, FR-S9): kept straight from the
 * copy's URL, confirmed to the server when whole, confirmed again until the
 * server has heard, and released when taken off the device.
 */

const ALICE = 1
const TRIP = 77
const SMALL = 'episode-9001-o.mp4'
/** The other slot a replacement downloads into (M20.4 E). */
const SMALL_B = 'episode-9001-o-b.mp4'
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

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(
      TRIP,
      9001,
      '"o"',
      TEST_DEVICE_ID,
      expect.anything(),
    )
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

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(
      TRIP,
      9001,
      '"o"',
      TEST_DEVICE_ID,
      expect.anything(),
    )
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

  it('stops asking, and keeps the file, when another device keeps the trip (owner, 2026-10-30)', async () => {
    const confirmDelivered = vi.fn(() =>
      Promise.reject(new ApiError(409, { detail: 'trip_other_device' })),
    )
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
    harness.manager.nudge()
    expect(confirmDelivered).toHaveBeenCalledTimes(1)
    expect(harness.manager.record(9001)).toMatchObject({ state: 'downloaded', tripId: TRIP })
    expect(harness.removed).toEqual([])
    expect(harness.files.get(SMALL)).toBe(210)
  })

  it('keeps asking after any other 409', async () => {
    const confirmDelivered = vi
      .fn<() => Promise<unknown>>()
      .mockRejectedValueOnce(new ApiError(409, { detail: 'trip_not_active' }))
      .mockResolvedValue(null)
    const harness = managerHarness({
      confirmDelivered,
      initial: [recordEntry(tripRecord({ confirm: 'pending' }))],
    })
    harness.files.set(SMALL, 210)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()
    await vi.waitFor(() => {
      expect(confirmDelivered).toHaveBeenCalledTimes(1)
    })
    await Promise.resolve()
    expect(harness.manager.record(9001)?.confirm).toBe('pending')

    harness.manager.confirmPending()
    await vi.waitFor(() => {
      expect(harness.manager.record(9001)?.confirm).toBe('done')
    })
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

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(
      TRIP,
      9001,
      '"e"',
      TEST_DEVICE_ID,
      expect.anything(),
    )
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

describe('a trip copy re-made on the server (owner, 2026-10-28)', () => {
  /** Alice holds the small copy `"o"`, already confirmed to the server. */
  async function holding(
    patch: Partial<DownloadRecord> = {},
    options: Parameters<typeof managerHarness>[0] = {},
  ) {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const record = tripRecord({ confirm: 'done', ...patch })
    const harness = managerHarness({
      confirmDelivered,
      ...options,
      initial: [recordEntry(record)],
    })
    harness.files.set(record.name, record.bytes)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()
    return { ...harness, confirmDelivered }
  }

  it('downloads the new copy beside the old, and drops the old only once the new is whole', async () => {
    const { manager, worker, removed, confirmDelivered } = await holding()

    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')

    expect(worker.lastCommand()).toMatchObject({
      cmd: 'download',
      name: SMALL_B,
      url: COPY_URL,
      etag: null,
      fresh: true,
    })
    expect(removed).toEqual([])
    expect(manager.record(9001)).toMatchObject({
      tripId: TRIP,
      variant: 'small',
      state: 'downloading',
      replacedFor: '"n"',
      previous: { name: SMALL, etag: '"o"', total: 210 },
    })
    // The old copy still plays, and still counts as held.
    expect(manager.isOnDevice(9001)).toBe(true)
    expect(await manager.playableUrl(9001)).toBe(`blob:${SMALL}`)

    worker.emit({ type: 'done', name: SMALL_B, offset: 300, total: 300, etag: '"n"', run: 1 })

    await vi.waitFor(() => {
      expect(removed).toEqual([SMALL])
    })
    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, '"n"', TEST_DEVICE_ID, {
      sha256: null,
      size: 300,
      variant: 'small',
    })
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', etag: '"n"', name: SMALL_B })
    expect(manager.record(9001)?.previous).toBeUndefined()
  })

  it('fetches the new copy once per server ETag, even if it lands under another', async () => {
    const { manager, worker } = await holding()
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    expect(worker.commands).toHaveLength(1)
    worker.emit({ type: 'done', name: SMALL_B, offset: 300, total: 300, etag: '"x"', run: 1 })

    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')

    expect(worker.commands).toHaveLength(1)
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', etag: '"x"' })
  })

  it('keeps the old copy exactly as it was when the new one is refused', async () => {
    const { manager, worker, removed, files } = await holding()
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')

    worker.emit({
      type: 'failed',
      name: SMALL_B,
      offset: 0,
      code: 'gone',
      reason: 'the server no longer has this episode',
      intact: true,
      run: 1,
    })

    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({
        state: 'downloaded',
        name: SMALL,
        etag: '"o"',
        total: 210,
        bytes: 210,
        confirm: 'done',
        replacedFor: '"n"',
      })
    })
    expect(manager.record(9001)?.previous).toBeUndefined()
    expect(files.get(SMALL)).toBe(210)
    expect(removed).toEqual([SMALL_B])
    // Not chased again for the same ETag, and not asked of the server again.
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    expect(worker.commands).toHaveLength(1)
  })

  it('keeps the old copy when the replacement is paused by hand', async () => {
    const { manager, worker, removed } = await holding()
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')

    manager.pause(9001)

    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({ state: 'downloaded', name: SMALL, etag: '"o"' })
    })
    // A pause by hand keeps `replacedFor`: only the server's offer retries.
    expect(manager.record(9001)?.replacedFor).toBe('"n"')
    worker.emit({ type: 'paused', name: SMALL_B, offset: 0, intact: true, run: 1 })
    await vi.waitFor(() => {
      expect(removed).toEqual([SMALL_B])
    })
    expect(removed).not.toContain(SMALL)
  })

  function midReplacement(patch: Partial<DownloadRecord> = {}): DownloadRecord {
    return tripRecord({
      confirm: 'done',
      name: SMALL_B,
      state: 'downloading',
      etag: '"n"',
      bytes: 40,
      total: 300,
      fresh: false,
      replacedFor: '"n"',
      previous: { name: SMALL, etag: '"o"', total: 210, sha256: null, confirm: 'done' },
      ...patch,
    })
  }

  async function relaunch(record: DownloadRecord, files: Record<string, number>) {
    const harness = managerHarness({ initial: [recordEntry(record)] })
    for (const [name, size] of Object.entries(files)) harness.files.set(name, size)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()
    return harness
  }

  it('resumes a half-done replacement after a launch, the old copy playing meanwhile', async () => {
    const { manager, removed } = await relaunch(midReplacement(), { [SMALL]: 210, [SMALL_B]: 40 })

    expect(manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'interrupted',
      autoResume: true,
      name: SMALL_B,
      bytes: 40,
      previous: { name: SMALL },
    })
    expect(await manager.playableUrl(9001)).toBe(`blob:${SMALL}`)
    expect(removed).toEqual([])
  })

  it('undoes a replacement that had nothing yet, and sweeps its empty file', async () => {
    const { manager, removed } = await relaunch(midReplacement({ bytes: 0, total: 0 }), {
      [SMALL]: 210,
      [SMALL_B]: 0,
    })

    expect(manager.record(9001)).toMatchObject({
      state: 'downloaded',
      name: SMALL,
      etag: '"o"',
      bytes: 210,
      replacedFor: '"n"',
    })
    expect(removed).toEqual([SMALL_B])
  })

  it('adopts a whole new copy whose record was saved before the old file went', async () => {
    const { manager, removed } = await relaunch(midReplacement({ bytes: 300 }), { [SMALL_B]: 300 })

    expect(manager.record(9001)).toMatchObject({
      state: 'downloaded',
      name: SMALL_B,
      etag: '"n"',
      bytes: 300,
      // Confirmed at launch, with the new ETag.
      confirm: 'done',
    })
    expect(manager.record(9001)?.previous).toBeUndefined()
    expect(removed).toEqual([])
  })

  it('keeps a copy whose ETag is the server’s', async () => {
    const { manager, worker, removed } = await holding()

    manager.adoptTrip(9001, TRIP, COPY_URL, '"o"')

    expect(worker.commands).toEqual([])
    expect(removed).toEqual([])
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', etag: '"o"' })
  })

  it('keeps it when either ETag is unknown, or there is no URL', async () => {
    const { manager, worker, removed } = await holding({ etag: null })

    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    expect(worker.commands).toEqual([])

    const second = await holding()
    second.manager.adoptTrip(9001, TRIP, COPY_URL, null)
    second.manager.adoptTrip(9001, TRIP, COPY_URL)
    second.manager.adoptTrip(9001, TRIP, null, '"n"')

    expect(second.worker.commands).toEqual([])
    expect([...removed, ...second.removed]).toEqual([])
    expect(second.manager.record(9001)).toMatchObject({ state: 'downloaded', etag: '"o"' })
  })

  it('confirms a full-size copy at once, whatever the ETag', async () => {
    const confirmDelivered = vi.fn(() => Promise.resolve(null))
    const full = downloadedRecord(ALICE)
    const harness = managerHarness({ confirmDelivered, initial: [recordEntry(full)] })
    harness.files.set(full.name, full.bytes)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    harness.manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')

    expect(confirmDelivered).toHaveBeenCalledExactlyOnceWith(
      TRIP,
      9001,
      '"e"',
      TEST_DEVICE_ID,
      expect.anything(),
    )
    expect(harness.worker.commands).toEqual([])
    expect(harness.removed).toEqual([])
  })

  it('replaces through keepTripCopy too, when the record is already there', async () => {
    const { manager, worker } = await holding()

    await manager.keepTripCopy({ episodeId: 9001, tripId: TRIP, url: COPY_URL, etag: '"n"' })

    await vi.waitFor(() => {
      expect(worker.lastCommand()).toMatchObject({ url: COPY_URL, fresh: true })
    })
  })

  it('confirms the new copy even if the old one’s confirmation was still in flight', async () => {
    const first = deferred()
    const confirmDelivered = vi
      .fn<(tripId: number, episodeId: number, etag: string | null) => Promise<unknown>>()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue(null)
    const { manager, worker } = await holding({ confirm: 'pending' }, { confirmDelivered })
    expect(confirmDelivered).toHaveBeenLastCalledWith(
      TRIP,
      9001,
      '"o"',
      TEST_DEVICE_ID,
      expect.anything(),
    )

    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    await vi.waitFor(() => {
      expect(worker.commands).toHaveLength(1)
    })
    worker.emit({ type: 'done', name: SMALL_B, offset: 300, total: 300, etag: '"n"', run: 1 })
    first.resolve()

    await vi.waitFor(() => {
      expect(confirmDelivered).toHaveBeenLastCalledWith(
        TRIP,
        9001,
        '"n"',
        TEST_DEVICE_ID,
        expect.anything(),
      )
    })
    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({ etag: '"n"', confirm: 'done' })
    })
  })

  it('waits while the episode is open in the player', async () => {
    const { manager, worker } = await holding()
    const leave = manager.enterPlayer(9001)

    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    expect(worker.commands).toEqual([])

    leave()
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    await vi.waitFor(() => {
      expect(worker.lastCommand()).toMatchObject({ url: COPY_URL, fresh: true })
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

    expect(releaseDelivered).toHaveBeenCalledExactlyOnceWith(TRIP, 9001, TEST_DEVICE_ID)
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
