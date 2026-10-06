import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import type { OfflineCopyOut } from '@/lib/anime'
import { codecsPlayable, MESSAGES, POLL_MS, type DownloadRecord } from '@/offline/downloads'
import { EPISODE_FILE, fileNameFor } from '@/offline/opfs'
import { PLAY_INFO } from '@/test/animeFixtures'
import {
  downloadedRecord,
  managerHarness,
  NO_SMALL_COPY,
  recordEntry,
  smallCopy,
} from '@/test/downloadFixtures'

/**
 * Keep offline takes the server's small copy (M19, FR-S9, owner 2026-10-05):
 * every branch of the start, the polling, the codec check, and the rule that
 * the two copies of one episode never touch each other's file.
 */

const ALICE = 1
const BOB = 2
const FULL = 'episode-9001.mp4'
const SMALL = 'episode-9001-o.mp4'
const FULL_URL = '/media/9001/episode.mp4'
const SMALL_URL = '/media/9001/offline.mp4'

function preparing(progress: number | null = null): OfflineCopyOut {
  return { state: 'preparing', progress, size: null, url: null, codecs: 'avc1.640028' }
}

/** A function answering each call with the next answer; the last one repeats. */
function answers(...replies: (OfflineCopyOut | Error)[]) {
  let index = 0
  return vi.fn(() => {
    const reply = replies[Math.min(index, replies.length - 1)]
    index += 1
    if (reply === undefined) return Promise.reject(new Error('no answer configured'))
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply)
  })
}

async function alice(options: Parameters<typeof managerHarness>[0] = {}) {
  const harness = managerHarness(options)
  harness.manager.setOwner(ALICE)
  await harness.manager.hydrate()
  return harness
}

afterEach(() => {
  vi.useRealTimers()
})

describe('file names per copy', () => {
  it('names the full and the small copy apart, and the sweep knows both', () => {
    expect(fileNameFor(9001)).toBe(FULL)
    expect(fileNameFor(9001, 'full')).toBe(FULL)
    expect(fileNameFor(9001, 'small')).toBe(SMALL)
    expect(EPISODE_FILE.test(FULL)).toBe(true)
    expect(EPISODE_FILE.test(SMALL)).toBe(true)
    expect(EPISODE_FILE.test('episode-9001-x.mp4')).toBe(false)
    expect(EPISODE_FILE.test('../episode-9001.mp4')).toBe(false)
  })
})

describe('the codec check', () => {
  it('asks the video element about the exact codecs and reads an empty answer as no', () => {
    const asked: string[] = []
    const probe = (type: string) => {
      asked.push(type)
      return type.includes('hvc1') ? '' : 'probably'
    }

    expect(codecsPlayable('avc1.640028', probe)).toBe(true)
    expect(codecsPlayable('hvc1.1.6.L93.B0', probe)).toBe(false)
    expect(asked).toEqual([
      'video/mp4; codecs="avc1.640028"',
      'video/mp4; codecs="hvc1.1.6.L93.B0"',
    ])
    expect(codecsPlayable('avc1.640028', () => 'maybe')).toBe(true)
  })

  it('takes a copy that names no codecs on trust', () => {
    const probe = vi.fn(() => '')
    expect(codecsPlayable(null, probe)).toBe(true)
    expect(codecsPlayable('  ', probe)).toBe(true)
    expect(probe).not.toHaveBeenCalled()
  })
})

describe('start() asks for the small copy', () => {
  it('downloads an available small copy from the URL the server gave', async () => {
    const requestCopy = answers(smallCopy(9001))
    const { manager, worker } = await alice({ requestCopy })

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(requestCopy).toHaveBeenCalledWith(9001)
    expect(worker.commands).toEqual([
      {
        cmd: 'download',
        name: SMALL,
        url: SMALL_URL,
        etag: null,
        total: null,
        fresh: true,
        run: 1,
      },
    ])
    expect(manager.record(9001)).toMatchObject({
      state: 'downloading',
      variant: 'small',
      name: SMALL,
      url: SMALL_URL,
      fullUrl: FULL_URL,
    })

    worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"' })
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', variant: 'small' })
  })

  it('takes the full-size file when the server no longer has the source (409)', async () => {
    const requestCopy = answers(new ApiError(409, { detail: 'source_gone' }))
    const { manager, worker } = await alice({ requestCopy })

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: FULL, url: FULL_URL })
    expect(manager.record(9001)).toMatchObject({ variant: 'full', name: FULL, url: FULL_URL })
  })

  it('takes the full-size file when the copy is unavailable', async () => {
    const { manager, worker } = await alice({ requestCopy: answers(NO_SMALL_COPY) })

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(worker.lastCommand()).toMatchObject({ name: FULL, url: FULL_URL })
    expect(manager.record(9001)?.variant).toBe('full')
  })

  it('takes the full-size file, saying nothing, when this device cannot play the copy', async () => {
    const hevc = smallCopy(9001, { codecs: 'hvc1.1.6.L93.B0' })
    const canPlayType = vi.fn((type: string) => (type.includes('hvc1') ? '' : 'probably'))
    const { manager, worker } = await alice({ requestCopy: answers(hevc), canPlayType })

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(canPlayType).toHaveBeenCalledWith('video/mp4; codecs="hvc1.1.6.L93.B0"')
    expect(worker.lastCommand()).toMatchObject({ name: FULL, url: FULL_URL })
    expect(manager.record(9001)).toMatchObject({ variant: 'full', message: null, reason: null })
  })

  it('says the server could not make the copy, and Try again asks again', async () => {
    const failed: OfflineCopyOut = { ...preparing(), state: 'failed' }
    const requestCopy = answers(failed, smallCopy(9001))
    const { manager, worker } = await alice({ requestCopy })

    await manager.start({ episodeId: 9001, url: FULL_URL })
    expect(manager.record(9001)).toMatchObject({
      state: 'failed',
      reason: 'unprepared',
      message: MESSAGES.unprepared,
      url: null,
    })
    expect(worker.commands).toEqual([])

    manager.resume(9001)
    await vi.waitFor(() => {
      expect(manager.record(9001)?.state).toBe('downloading')
    })
    expect(requestCopy).toHaveBeenCalledTimes(2)
    expect(worker.lastCommand()).toMatchObject({ name: SMALL, url: SMALL_URL })
  })

  it.each([
    [409, 'storage_held'],
    [429, 'copy_queue_full'],
  ])('takes the full-size file on %i %s', async (status, detail) => {
    const requestCopy = answers(new ApiError(status, { detail }))
    const { manager, worker } = await alice({ requestCopy })

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: FULL, url: FULL_URL })
    expect(manager.record(9001)).toMatchObject({ variant: 'full', reason: null })
  })

  it('does not take a 429 or 409 with another detail as a fallback', async () => {
    const { manager, worker } = await alice({
      requestCopy: answers(new ApiError(429, { detail: 'slow down' })),
    })

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'error' })
    expect(worker.commands).toEqual([])
  })

  it('fails with the sentence for a refusal: 403 is auth, 404 is gone', async () => {
    const refused = await alice({ requestCopy: answers(new ApiError(403, null)) })
    await refused.manager.start({ episodeId: 9001, url: FULL_URL })
    expect(refused.manager.record(9001)).toMatchObject({ state: 'failed', reason: 'auth' })

    const gone = await alice({ requestCopy: answers(new ApiError(404, null)) })
    await gone.manager.start({ episodeId: 9001, url: FULL_URL })
    expect(gone.manager.record(9001)).toMatchObject({
      state: 'failed',
      reason: 'gone',
      message: MESSAGES.gone,
    })
  })

  it('rejects, as before, only when the episode payload cannot be had', async () => {
    const requestCopy = answers(smallCopy(4242))
    const { manager } = await alice({ requestCopy })

    await expect(
      manager.start({ episodeId: 4242, url: '/media/4242/episode.mp4' }),
    ).rejects.toThrow()
    expect(requestCopy).not.toHaveBeenCalled()
    expect(manager.record(4242)).toBeUndefined()
  })
})

describe('waiting for the server', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('polls every 20 s while preparing, shows the server percent, then downloads', async () => {
    const pollCopy = answers(preparing(0.4), smallCopy(9001))
    const { manager, worker } = await alice({
      requestCopy: answers(preparing(null)),
      pollCopy,
    })

    await manager.start({ episodeId: 9001, url: FULL_URL })
    expect(manager.record(9001)).toMatchObject({
      state: 'preparing',
      serverProgress: null,
      variant: 'small',
      url: null,
    })
    expect(worker.commands).toEqual([])

    await vi.advanceTimersByTimeAsync(POLL_MS - 1)
    expect(pollCopy).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(pollCopy).toHaveBeenCalledTimes(1)
    expect(manager.record(9001)).toMatchObject({ state: 'preparing', serverProgress: 0.4 })

    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(pollCopy).toHaveBeenCalledTimes(2)
    expect(manager.record(9001)).toMatchObject({ state: 'downloading', name: SMALL })
    expect(worker.lastCommand()).toMatchObject({ name: SMALL, url: SMALL_URL })

    // Nothing waits on the server any more: the polling has stopped.
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(pollCopy).toHaveBeenCalledTimes(2)
  })

  it('lands a poll that turns failed after preparing in failed, and Try again asks again', async () => {
    const failed: OfflineCopyOut = { ...preparing(), state: 'failed' }
    const requestCopy = answers(preparing(0.3), smallCopy(9001))
    const { manager, worker } = await alice({ requestCopy, pollCopy: answers(failed) })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    await vi.advanceTimersByTimeAsync(POLL_MS)

    expect(manager.record(9001)).toMatchObject({
      state: 'failed',
      reason: 'unprepared',
      message: MESSAGES.unprepared,
      url: null,
    })
    // Nothing waits on the server: no more polls.
    await vi.advanceTimersByTimeAsync(POLL_MS * 2)
    expect(requestCopy).toHaveBeenCalledTimes(1)

    manager.resume(9001)
    await vi.advanceTimersByTimeAsync(0)
    expect(requestCopy).toHaveBeenCalledTimes(2)
    expect(worker.lastCommand()).toMatchObject({ name: SMALL, url: SMALL_URL })
  })

  it('treats a queued copy the same as one being made', async () => {
    const queued: OfflineCopyOut = { ...preparing(), state: 'queued' }
    const { manager } = await alice({ requestCopy: answers(queued) })

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(manager.record(9001)?.state).toBe('preparing')
  })

  it('waits while the page is hidden, and asks at once on a nudge', async () => {
    let visible = false
    const pollCopy = answers(preparing(0.1))
    const { manager } = await alice({
      requestCopy: answers(preparing()),
      pollCopy,
      isVisible: () => visible,
    })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(pollCopy).not.toHaveBeenCalled()

    visible = true
    manager.nudge()
    await vi.advanceTimersByTimeAsync(0)
    expect(pollCopy).toHaveBeenCalledTimes(1)
    expect(manager.record(9001)?.serverProgress).toBe(0.1)
  })

  it('falls back to the full file when a poll finds the copy unavailable', async () => {
    const { manager, worker } = await alice({
      requestCopy: answers(preparing()),
      pollCopy: answers(NO_SMALL_COPY),
    })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    await vi.advanceTimersByTimeAsync(POLL_MS)

    expect(manager.record(9001)).toMatchObject({ state: 'downloading', variant: 'full' })
    expect(worker.lastCommand()).toMatchObject({ name: FULL, url: FULL_URL })
  })

  it('asks again when a poll finds no copy at all on the server', async () => {
    const none: OfflineCopyOut = { ...preparing(), state: 'none' }
    const requestCopy = answers(preparing(), smallCopy(9001))
    const { manager } = await alice({ requestCopy, pollCopy: answers(none) })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    await vi.advanceTimersByTimeAsync(POLL_MS)

    expect(requestCopy).toHaveBeenCalledTimes(2)
    expect(manager.record(9001)).toMatchObject({ state: 'downloading', variant: 'small' })
  })

  it('keeps waiting through a poll that got no answer', async () => {
    const pollCopy = answers(new TypeError('Load failed'), preparing(0.7))
    const { manager } = await alice({ requestCopy: answers(preparing()), pollCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(manager.record(9001)?.state).toBe('preparing')
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(manager.record(9001)).toMatchObject({ state: 'preparing', serverProgress: 0.7 })
  })

  it('never drops a request that got no answer: it waits for a connection and asks again', async () => {
    const requestCopy = answers(new TypeError('Load failed'), smallCopy(9001))
    const { manager, worker } = await alice({ requestCopy })

    await manager.start({ episodeId: 9001, url: FULL_URL })
    expect(manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'network',
      message: MESSAGES.network,
      url: null,
    })

    await vi.advanceTimersByTimeAsync(POLL_MS)

    expect(requestCopy).toHaveBeenCalledTimes(2)
    expect(manager.record(9001)).toMatchObject({ state: 'downloading', variant: 'small' })
    expect(worker.lastCommand()).toMatchObject({ name: SMALL })
  })

  it('stops polling on a pause, and asks again on resume', async () => {
    const requestCopy = answers(preparing(), smallCopy(9001))
    const pollCopy = answers(preparing(0.2))
    const { manager } = await alice({ requestCopy, pollCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    manager.pause(9001)
    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'by-hand' })
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(pollCopy).not.toHaveBeenCalled()

    manager.resume(9001)
    await vi.advanceTimersByTimeAsync(0)
    expect(requestCopy).toHaveBeenCalledTimes(2)
    expect(manager.record(9001)).toMatchObject({ state: 'downloading', variant: 'small' })
  })

  it('stops polling when the wish is removed, and makes no call to the server about it', async () => {
    const requestCopy = answers(preparing())
    const pollCopy = answers(preparing(0.2))
    const { manager, files, removed } = await alice({ requestCopy, pollCopy })
    files.set(FULL, 1000)
    await manager.start({ episodeId: 9001, url: FULL_URL })

    await manager.remove(9001)

    expect(manager.record(9001)).toBeUndefined()
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(pollCopy).not.toHaveBeenCalled()
    expect(requestCopy).toHaveBeenCalledTimes(1)
    expect(removed).not.toContain(FULL)
  })

  it('drops an answer that lands after a pause', async () => {
    let answer: (copy: OfflineCopyOut) => void = () => undefined
    const requestCopy = vi.fn(
      () =>
        new Promise<OfflineCopyOut>((resolve) => {
          answer = resolve
        }),
    )
    const { manager, worker } = await alice({ requestCopy })
    const starting = manager.start({ episodeId: 9001, url: FULL_URL })
    await vi.advanceTimersByTimeAsync(0)
    expect(manager.record(9001)?.state).toBe('preparing')

    manager.pause(9001)
    answer(smallCopy(9001))
    await starting

    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'by-hand' })
    expect(worker.commands).toEqual([])
  })

  it('does not poll for another account, and picks it up again when it is back', async () => {
    const pollCopy = answers(preparing(0.5))
    const { manager } = await alice({ requestCopy: answers(preparing()), pollCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    manager.setOwner(BOB)
    await vi.advanceTimersByTimeAsync(POLL_MS * 2)
    expect(pollCopy).not.toHaveBeenCalled()

    // Back: it asks at once, then on the beat.
    manager.setOwner(ALICE)
    await vi.advanceTimersByTimeAsync(0)
    expect(pollCopy).toHaveBeenCalledTimes(1)
    expect(manager.record(9001)).toMatchObject({ state: 'preparing', serverProgress: 0.5 })
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(pollCopy).toHaveBeenCalledTimes(2)
  })
})

describe('hydrate', () => {
  it('reads a record from before M19 as the full-size copy', async () => {
    const legacy: Partial<DownloadRecord> = { ...downloadedRecord(ALICE) }
    delete legacy.variant
    const { manager, files } = managerHarness({ initial: [['1:9001', legacy]] })
    files.set(FULL, 1000)
    manager.setOwner(ALICE)

    await manager.hydrate()

    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', variant: 'full' })
    expect(await manager.playableUrl(9001)).toBe(`blob:${FULL}`)
  })

  it('keeps a preparing record preparing and asks the server about it', async () => {
    const waiting: DownloadRecord = {
      ...downloadedRecord(ALICE),
      name: SMALL,
      url: null,
      variant: 'small',
      fullUrl: FULL_URL,
      state: 'preparing',
      bytes: 0,
      total: 0,
      etag: null,
    }
    const pollCopy = answers(smallCopy(9001))
    const { manager, worker } = managerHarness({ initial: [recordEntry(waiting)], pollCopy })
    manager.setOwner(ALICE)

    await manager.hydrate()

    await vi.waitFor(() => {
      expect(manager.record(9001)?.state).toBe('downloading')
    })
    expect(pollCopy).toHaveBeenCalledTimes(1)
    expect(worker.lastCommand()).toMatchObject({ name: SMALL, url: SMALL_URL })
  })

  it('sweeps an orphan small copy and keeps the ones records name', async () => {
    const small = {
      ...downloadedRecord(ALICE),
      name: SMALL,
      url: SMALL_URL,
      variant: 'small' as const,
    }
    const { manager, files, removed } = managerHarness({ initial: [recordEntry(small)] })
    files.set(SMALL, 1000)
    files.set('episode-9002-o.mp4', 50)

    await manager.hydrate()

    expect(removed).toEqual(['episode-9002-o.mp4'])
    expect(files.has(SMALL)).toBe(true)
  })
})

describe('two accounts, two copies of one episode', () => {
  it("a small-copy start never touches another account's full copy", async () => {
    const { manager, worker, files, removed } = managerHarness({
      initial: [recordEntry(downloadedRecord(ALICE))],
      requestCopy: answers(smallCopy(9001)),
    })
    files.set(FULL, 1000)
    await manager.hydrate()
    manager.setOwner(BOB)

    await manager.start({ episodeId: 9001, url: FULL_URL })

    // Its own file, fresh: no resume into Alice's bytes, no If-Range on them.
    expect(worker.lastCommand()).toEqual({
      cmd: 'download',
      name: SMALL,
      url: SMALL_URL,
      etag: null,
      total: null,
      fresh: true,
      run: 1,
    })
    worker.emit({ type: 'restarted', name: SMALL, reason: 'replaced' })
    worker.emit({ type: 'done', name: SMALL, offset: 210, total: 210, etag: '"o"' })
    files.set(SMALL, 210)

    await manager.remove(9001)
    expect(removed).toEqual([SMALL])
    expect(files.get(FULL)).toBe(1000)

    manager.setOwner(ALICE)
    expect(manager.record(9001)).toMatchObject({
      state: 'downloaded',
      variant: 'full',
      bytes: 1000,
      total: 1000,
      etag: '"e"',
    })
  })

  it("a full-size start never touches another account's small copy", async () => {
    const small = {
      ...downloadedRecord(ALICE),
      name: SMALL,
      url: SMALL_URL,
      variant: 'small' as const,
    }
    const { manager, worker, files, removed } = managerHarness({
      initial: [recordEntry(small)],
      requestCopy: answers(new ApiError(409, { detail: 'source_gone' })),
    })
    files.set(SMALL, 1000)
    await manager.hydrate()
    manager.setOwner(BOB)

    await manager.start({ episodeId: 9001, url: FULL_URL })

    expect(worker.lastCommand()).toMatchObject({ name: FULL, fresh: true, etag: null })
    await manager.remove(9001)
    // Running: its own file goes once the worker has let go of it.
    worker.emit({ type: 'paused', name: FULL, offset: 0, run: 1 })
    expect(removed).toEqual([FULL])
    expect(files.get(SMALL)).toBe(1000)
  })

  it('shares the same copy between accounts, as before', async () => {
    const small = {
      ...downloadedRecord(ALICE),
      name: SMALL,
      url: SMALL_URL,
      variant: 'small' as const,
    }
    const { manager, worker, files } = managerHarness({
      initial: [recordEntry(small)],
      requestCopy: answers(smallCopy(9001)),
    })
    files.set(SMALL, 1000)
    await manager.hydrate()
    manager.setOwner(BOB)

    await manager.start({ episodeId: 9001, url: FULL_URL })

    // Vouched for by Alice's record: confirmed with the server, not refetched.
    expect(worker.lastCommand()).toMatchObject({
      name: SMALL,
      fresh: false,
      etag: '"e"',
      total: 1000,
    })
  })

  it('a record still preparing vouches for nothing on disk', async () => {
    const { manager, files, removed } = managerHarness({
      initial: [
        recordEntry({ ...downloadedRecord(ALICE), name: SMALL, url: SMALL_URL, variant: 'small' }),
      ],
      requestCopy: answers(preparing()),
    })
    files.set(SMALL, 1000)
    await manager.hydrate()
    manager.setOwner(BOB)
    await manager.start({ episodeId: 9001, url: FULL_URL })
    expect(manager.record(9001)?.state).toBe('preparing')

    // Alice deletes hers: Bob's wish names the file but holds none of it.
    manager.setOwner(ALICE)
    await manager.remove(9001)
    expect(removed).toEqual([SMALL])
  })
})

describe('a small copy the server no longer has', () => {
  const gone = (run: number) =>
    ({ type: 'failed', name: SMALL, offset: 0, code: 'gone', reason: 'x', run }) as const

  it('asks the server again by itself once, and restarts a fresh download from its answer', async () => {
    const requestCopy = answers(smallCopy(9001))
    const { manager, worker } = await alice({ requestCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })
    worker.emit({ type: 'progress', name: SMALL, offset: 400, total: 1000, etag: '"o"', run: 1 })

    worker.emit(gone(1))

    await vi.waitFor(() => {
      expect(manager.record(9001)?.state).toBe('downloading')
    })
    expect(requestCopy).toHaveBeenCalledTimes(2)
    expect(worker.lastCommand()).toMatchObject({
      name: SMALL,
      url: SMALL_URL,
      fresh: true,
      etag: null,
      run: 2,
    })
    expect(manager.record(9001)?.reasked).toBe(true)
  })

  it('waits for the server when the re-ask finds the copy being made again', async () => {
    const requestCopy = answers(smallCopy(9001), preparing(0.1))
    const { manager, worker } = await alice({ requestCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    worker.emit(gone(1))

    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({ state: 'preparing', serverProgress: 0.1 })
    })
    expect(worker.commands).toHaveLength(1)
  })

  it('takes the full file when the re-ask finds the source gone', async () => {
    const requestCopy = answers(smallCopy(9001), new ApiError(409, { detail: 'source_gone' }))
    const { manager, worker } = await alice({ requestCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    worker.emit(gone(1))

    await vi.waitFor(() => {
      expect(manager.record(9001)).toMatchObject({ state: 'downloading', variant: 'full' })
    })
    expect(worker.lastCommand()).toMatchObject({ name: FULL, url: FULL_URL })
  })

  it('stops at "gone" the second time, and Try again asks the server', async () => {
    const requestCopy = answers(smallCopy(9001))
    const { manager, worker } = await alice({ requestCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })
    worker.emit(gone(1))
    await vi.waitFor(() => {
      expect(manager.record(9001)?.state).toBe('downloading')
    })

    worker.emit(gone(2))

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'gone' })
    expect(requestCopy).toHaveBeenCalledTimes(2)

    manager.resume(9001)
    await vi.waitFor(() => {
      expect(manager.record(9001)?.state).toBe('downloading')
    })
    expect(requestCopy).toHaveBeenCalledTimes(3)
    expect(worker.lastCommand()).toMatchObject({ name: SMALL, fresh: true, run: 3 })
  })

  it('may ask again by itself after a download has finished since', async () => {
    const requestCopy = answers(smallCopy(9001))
    const { manager, worker } = await alice({ requestCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })
    worker.emit(gone(1))
    await vi.waitFor(() => {
      expect(manager.record(9001)?.state).toBe('downloading')
    })
    worker.emit({ type: 'done', name: SMALL, offset: 10, total: 10, etag: '"o"', run: 2 })
    expect(manager.record(9001)?.reasked).toBe(false)
  })

  it('leaves a full-size download that gets a 404 at "gone"', async () => {
    const requestCopy = answers(NO_SMALL_COPY)
    const { manager, worker } = await alice({ requestCopy })
    await manager.start({ episodeId: 9001, url: FULL_URL })

    worker.emit({ type: 'failed', name: FULL, offset: 0, code: 'gone', reason: 'x', run: 1 })

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'gone' })
    expect(requestCopy).toHaveBeenCalledTimes(1)
  })

  it('plays a full-size record whose snapshot is the episode, unchanged', async () => {
    const { manager, files } = managerHarness({ initial: [recordEntry(downloadedRecord(ALICE))] })
    files.set(FULL, 1000)
    manager.setOwner(ALICE)
    await manager.hydrate()

    expect(manager.record(9001)?.snapshot.episode.id).toBe(PLAY_INFO.episode.id)
    expect(await manager.playableUrl(9001)).toBe(`blob:${FULL}`)
  })
})
