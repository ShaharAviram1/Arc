import { describe, expect, it, vi } from 'vitest'
import { MESSAGES, type DownloadRecord } from '@/offline/downloads'
import { memoryStore } from '@/offline/store'
import { PLAY_INFO } from '@/test/animeFixtures'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

/**
 * The deletion invariant (M20.4 E fix loop): the only paths that remove a
 * whole held file are Remove, watched-removal, the server's copy_mismatch on
 * a small copy, and a replacement's drop of the old file once the new one is
 * durably recorded. Each test below is a way the file used to go.
 */

const ALICE = 1
const BOB = 2
const TRIP = 77
const SMALL = 'episode-9001-o.mp4'
const SMALL_B = 'episode-9001-o-b.mp4'
const SMALL_C = 'episode-9001-o-c.mp4'
const COPY_URL = '/media/9001/offline.mp4'

function small(patch: Partial<DownloadRecord> = {}): DownloadRecord {
  return {
    ...downloadedRecord(ALICE, PLAY_INFO, 210),
    name: SMALL,
    url: COPY_URL,
    variant: 'small',
    tripId: TRIP,
    etag: '"o"',
    confirm: 'done',
    ...patch,
  }
}

async function launch(
  records: DownloadRecord[],
  files: Record<string, number>,
  options: Parameters<typeof managerHarness>[0] = {},
) {
  const harness = managerHarness({ ...options, initial: records.map(recordEntry) })
  for (const [name, size] of Object.entries(files)) harness.files.set(name, size)
  harness.manager.setOwner(ALICE)
  await harness.manager.hydrate()
  return harness
}

describe('Try again on a whole file that failed for another reason', () => {
  it.each(['size', 'error'] as const)(
    'a whole small copy recorded failed/%s, answered 404, is downloaded again and survives a relaunch',
    async (reason) => {
      const failed = small({ state: 'failed', reason, message: MESSAGES[reason] })
      // At launch it reads short, so it stays failed; then it is whole again.
      const first = await launch([failed], { [SMALL]: 100 })
      first.files.set(SMALL, 210)

      first.manager.resume(9001)
      first.worker.emit({
        type: 'failed',
        name: SMALL,
        offset: 210,
        code: 'gone',
        reason: 'the server no longer has this episode',
        intact: true,
        run: 1,
      })

      expect(first.manager.record(9001)).toMatchObject({
        state: 'downloaded',
        url: COPY_URL,
        name: SMALL,
      })
      expect(first.removed).toEqual([])

      // Relaunch on the same storage: the file is still there.
      const again = managerHarness({ store: first.store })
      again.files.set(SMALL, 210)
      again.manager.setOwner(ALICE)
      await again.manager.hydrate()
      expect(again.removed).toEqual([])
      expect(again.manager.record(9001)?.state).toBe('downloaded')
    },
  )

  it('never asks again (and so never forgets the URL) for a file the run left with bytes', async () => {
    const requestCopy = vi.fn()
    const paused = small({ state: 'paused', reason: 'by-hand', bytes: 100, total: 210 })
    const { manager, worker } = await launch([paused], { [SMALL]: 100 }, { requestCopy })

    manager.resume(9001)
    worker.emit({
      type: 'failed',
      name: SMALL,
      offset: 100,
      code: 'gone',
      reason: '404',
      intact: true,
      run: 1,
    })

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'gone', url: COPY_URL })
    expect(requestCopy).not.toHaveBeenCalled()
  })

  it('the launch sweep spares a file named by a record with no URL yet', async () => {
    const waiting = small({ state: 'preparing', url: null, bytes: 0, total: 0 })
    const { removed } = await launch([waiting], { [SMALL]: 210 })
    expect(removed).toEqual([])
  })
})

describe('the launch reconcile', () => {
  it('adopts a whole copy found in another slot when its own is gone', async () => {
    const { manager, removed } = await launch([small()], { [SMALL_B]: 210 })
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', name: SMALL_B })
    expect(removed).toEqual([])
  })

  it('never makes an unreadable or mismatched copy downloaded again', async () => {
    for (const reason of ['unreadable', 'mismatch'] as const) {
      const failed = small({ state: 'failed', reason, fresh: true })
      const { manager } = await launch([failed], { [SMALL]: 210 })
      expect(manager.record(9001)).toMatchObject({ state: 'failed', reason })
    }
  })

  it('does not vouch for a whole file under a fresh record, but does for an evicted one', async () => {
    const fresh = small({ state: 'paused', reason: 'by-hand', fresh: true })
    const kept = await launch([fresh], { [SMALL]: 210 })
    expect(kept.manager.record(9001)?.state).toBe('paused')

    const evicted = small({ state: 'failed', reason: 'evicted', fresh: true })
    const back = await launch([evicted], { [SMALL]: 210 })
    expect(back.manager.record(9001)).toMatchObject({ state: 'downloaded', fresh: false })
  })

  it('shows the stored records while it reads a doubtful file again', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    let reads = 0
    const harness = managerHarness({
      initial: [recordEntry(small())],
      sizeOf: () => {
        reads += 1
        return reads === 1 ? Promise.resolve('missing' as const) : gate.then(() => 210)
      },
    })
    harness.manager.setOwner(ALICE)
    const hydrated = harness.manager.hydrate()

    await vi.waitFor(() => {
      expect(harness.manager.record(9001)?.state).toBe('downloaded')
    })
    release()
    await hydrated
    expect(harness.manager.record(9001)?.state).toBe('downloaded')
  })
})

describe('a replacement drops the old file only once the new one is on record', () => {
  async function replacing(options: Parameters<typeof managerHarness>[0] = {}) {
    const harness = await launch([small()], { [SMALL]: 210 }, options)
    harness.manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    return harness
  }

  const done = {
    type: 'done',
    name: SMALL_B,
    offset: 300,
    total: 300,
    etag: '"n"',
    run: 1,
  } as const

  it('keeps the old file when the storage is not persistent', async () => {
    const { manager, worker, removed } = await replacing({ durable: () => Promise.resolve(false) })
    worker.emit(done)
    await vi.waitFor(() => {
      expect(manager.record(9001)?.name).toBe(SMALL_B)
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(removed).toEqual([])
  })

  it('keeps the old file when the record could not be written', async () => {
    const store = memoryStore([recordEntry(small())])
    const put = store.put.bind(store)
    let failing = false
    store.put = (key, value) => (failing ? Promise.reject(new Error('quota')) : put(key, value))
    const { worker, removed } = await replacing({ store })
    failing = true
    worker.emit(done)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(removed).toEqual([])
  })

  it('waits for the player to be left before dropping the old file', async () => {
    const { manager, worker, removed } = await replacing()
    const leave = manager.enterPlayer(9001)
    worker.emit(done)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(removed).toEqual([])

    leave()
    await vi.waitFor(() => {
      expect(removed).toEqual([SMALL])
    })
  })
})

describe('a replacement and the slots', () => {
  it('never cuts another account’s copy in the other slot: it takes a free one', async () => {
    const bobs = { ...small({ userId: BOB, name: SMALL_B, etag: '"x"' }), tripId: undefined }
    const harness = managerHarness({ initial: [recordEntry(small()), recordEntry(bobs)] })
    harness.files.set(SMALL, 210)
    harness.files.set(SMALL_B, 210)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    harness.manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')

    expect(harness.worker.lastCommand()).toMatchObject({ name: SMALL_C, fresh: true })
  })

  it('shares another account’s copy of the same new ETag instead of fetching it', async () => {
    const bobs = { ...small({ userId: BOB, name: SMALL_B, etag: '"n"' }), tripId: undefined }
    const harness = managerHarness({ initial: [recordEntry(small()), recordEntry(bobs)] })
    harness.files.set(SMALL, 210)
    harness.files.set(SMALL_B, 210)
    harness.manager.setOwner(ALICE)
    await harness.manager.hydrate()

    harness.manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')

    expect(harness.worker.lastCommand()).toMatchObject({
      name: SMALL_B,
      fresh: false,
      etag: '"n"',
      total: 210,
    })
  })
})

describe('a replacement the device stops', () => {
  it('keeps its half-made file through an interruption and resumes into it', async () => {
    const { manager, worker, removed } = await launch([small()], { [SMALL]: 210 })
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    worker.emit({ type: 'progress', name: SMALL_B, offset: 40, total: 300, etag: '"n"', run: 1 })

    worker.emit({ type: 'paused', name: SMALL_B, offset: 40, reason: 'interrupted', run: 1 })

    expect(manager.record(9001)).toMatchObject({ name: SMALL_B, previous: { name: SMALL } })
    expect(removed).toEqual([])
    await vi.waitFor(() => {
      expect(manager.record(9001)?.state).toBe('downloading')
    })
    expect(manager.isOnDevice(9001)).toBe(true)
  })

  it('out of space: the old copy stays and the ETag is not chased again', async () => {
    const { manager, worker } = await launch([small()], { [SMALL]: 210 })
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    worker.emit({ type: 'paused', name: SMALL_B, offset: 0, reason: 'quota', run: 1 })

    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({
        state: 'downloaded',
        name: SMALL,
        replacedFor: '"n"',
      })
    })
  })

  it('another window writing the new file: its file is not dropped', async () => {
    const { manager, worker, removed } = await launch([small()], { [SMALL]: 210 })
    manager.adoptTrip(9001, TRIP, COPY_URL, '"n"')
    worker.emit({ type: 'busy', name: SMALL_B, run: 1 })

    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({ state: 'downloaded', name: SMALL })
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(removed).toEqual([])
  })
})

describe('rechecked is cleared once a copy checks', () => {
  it('on a done whose digest is the server’s', async () => {
    const { manager, worker } = await launch([], {})
    await manager.keepTripCopy({
      episodeId: 9001,
      tripId: TRIP,
      url: COPY_URL,
      sha256: 'a'.repeat(64),
    })
    const emit = (sha256: string, run: number) => {
      worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"', sha256, run })
    }
    emit('b'.repeat(64), 1)
    await vi.waitFor(() => {
      expect(manager.record(9001)?.rechecked).toBe(true)
    })
    await vi.waitFor(() => {
      expect(worker.commands).toHaveLength(2)
    })
    emit('a'.repeat(64), 2)
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', rechecked: false })
  })
})
