import { describe, expect, it } from 'vitest'
import { downloadedNeighbours, MESSAGES, QUIET_RETRIES } from '@/offline/downloads'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

const ALICE = 1
const BOB = 2
const NAME_1 = 'episode-9001.mp4'
const NAME_2 = 'episode-9002.mp4'

async function started(episodes: number[] = [9001]) {
  const harness = managerHarness()
  harness.manager.setOwner(ALICE)
  await harness.manager.hydrate()
  for (const episodeId of episodes) {
    await harness.manager.start({ episodeId, url: `/media/${String(episodeId)}/episode.mp4` })
  }
  return harness
}

describe('DownloadManager', () => {
  it('starts a download in the worker and remembers what it needs offline', async () => {
    const { manager, worker, covers } = await started()

    expect(worker.commands).toEqual([
      {
        cmd: 'download',
        name: NAME_1,
        url: '/media/9001/episode.mp4',
        etag: null,
        total: null,
        // No record vouched for anything on disk: the worker starts it over.
        fresh: true,
        run: 1,
      },
    ])
    const record = manager.record(9001)
    expect(record?.state).toBe('downloading')
    expect(record?.snapshot.anime.title.preferred).toBe(PLAY_INFO.anime.title.preferred)
    expect(record?.snapshot.episode.number).toBe(1)
    expect(record?.snapshot.duration).toBe(PLAY_INFO.duration)
    expect(covers.kept).toEqual([PLAY_INFO.anime.id])
  })

  it('follows progress to downloaded, and then plays from the file', async () => {
    const { manager, worker, files } = await started()

    worker.emit({ type: 'progress', name: NAME_1, offset: 400, total: 1000, etag: '"e"' })
    expect(manager.record(9001)).toMatchObject({ state: 'downloading', bytes: 400, total: 1000 })

    files.set(NAME_1, 1000)
    worker.emit({ type: 'done', name: NAME_1, offset: 1000, total: 1000, etag: '"e"' })
    expect(manager.record(9001)?.state).toBe('downloaded')
    expect(manager.isOnDevice(9001)).toBe(true)
    expect(manager.playableUrlNow(9001)).toBeNull()
    expect(await manager.playableUrl(9001)).toBe(`blob:${NAME_1}`)
    // Minted once, it is there for a tap without an await.
    expect(manager.playableUrlNow(9001)).toBe(`blob:${NAME_1}`)
  })

  it('runs one download at a time, in the order asked', async () => {
    const { manager, worker } = await started([9001, 9002])

    expect(manager.record(9002)?.state).toBe('queued')
    expect(worker.commands).toHaveLength(1)

    worker.emit({ type: 'done', name: NAME_1, offset: 10, total: 10, etag: null })
    expect(manager.record(9002)?.state).toBe('downloading')
    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_2 })
  })

  it('pauses by hand, lets the next one run, and resumes from the ETag it had', async () => {
    const { manager, worker } = await started([9001, 9002])
    worker.emit({ type: 'progress', name: NAME_1, offset: 400, total: 1000, etag: '"e"' })

    manager.pause(9001)
    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'by-hand' })
    expect(worker.commands.slice(-2)).toEqual([
      { cmd: 'pause', name: NAME_1 },
      expect.objectContaining({ cmd: 'download', name: NAME_2 }),
    ])

    worker.emit({ type: 'paused', name: NAME_1, offset: 400 })
    worker.emit({ type: 'done', name: NAME_2, offset: 10, total: 10, etag: null })

    manager.resume(9001)
    expect(manager.record(9001)?.state).toBe('downloading')
    expect(worker.lastCommand()).toEqual({
      cmd: 'download',
      name: NAME_1,
      url: '/media/9001/episode.mp4',
      etag: '"e"',
      total: 1000,
      fresh: false,
      run: 3,
    })
  })

  it('says it is waiting for a connection only once the quick retries are spent', async () => {
    const { manager, worker } = await started()

    for (let attempt = 1; attempt <= QUIET_RETRIES; attempt += 1) {
      worker.emit({ type: 'retrying', name: NAME_1, attempt, delay: 1000, reason: 'x' })
    }
    expect(manager.record(9001)?.state).toBe('downloading')

    worker.emit({ type: 'retrying', name: NAME_1, attempt: 4, delay: 30_000, reason: 'x' })
    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'network' })

    // Back online: the nudge goes to the worker and the row says downloading.
    manager.nudge()
    expect(manager.record(9001)?.state).toBe('downloading')
    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_1 })

    worker.emit({ type: 'progress', name: NAME_1, offset: 10, total: 20, etag: null })
    expect(manager.record(9001)?.state).toBe('downloading')
  })

  it('turns a full disk into a pause that says so, and holds the queue', async () => {
    const { manager, worker, workers } = await started([9001, 9002])

    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 800,
      reason: 'quota',
      detail: 'QuotaExceededError',
      run: 1,
    })

    expect(manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'quota',
      bytes: 800,
      message: MESSAGES.quota,
    })
    // Nothing else tries a first chunk only to fail the same way.
    expect(manager.record(9002)?.state).toBe('queued')
    expect(workers).toHaveLength(1)

    // Try again still works: it is the probe, run alone on a new worker.
    manager.resume(9001)
    expect(manager.record(9001)?.state).toBe('downloading')
    expect(workers).toHaveLength(2)
    expect(worker.terminated).toBe(true)
  })

  it('says a re-encode restarted the download', async () => {
    const { manager, worker } = await started()
    worker.emit({ type: 'progress', name: NAME_1, offset: 400, total: 1000, etag: '"e"' })

    worker.emit({ type: 'restarted', name: NAME_1, reason: 'replaced' })

    expect(manager.record(9001)).toMatchObject({ bytes: 0, total: 0, etag: null })
    expect(manager.record(9001)?.message).toMatch(/re-encoded/)
  })

  it('deletes a finished download, its file and, with the last one, the poster', async () => {
    const { manager, files, removed, covers, store } = managerHarness({
      initial: [recordEntry(downloadedRecord(ALICE))],
    })
    files.set(NAME_1, 1000)
    manager.setOwner(ALICE)
    await manager.hydrate()

    await manager.remove(9001)

    expect(manager.record(9001)).toBeUndefined()
    expect(removed).toEqual([NAME_1])
    expect(covers.dropped).toEqual([PLAY_INFO.anime.id])
    expect(await store.entries()).toEqual([])
  })

  it('deletes a running download only once the worker has let go of the file', async () => {
    const { manager, worker, removed } = await started()

    await manager.remove(9001)
    expect(worker.lastCommand()).toEqual({ cmd: 'pause', name: NAME_1 })
    expect(removed).toEqual([])

    worker.emit({ type: 'paused', name: NAME_1, offset: 400 })
    expect(removed).toEqual([NAME_1])
  })

  describe('hydrate', () => {
    it('says so when the device took a downloaded file back, and brings a mid-flight one back paused', async () => {
      const midFlight = {
        ...downloadedRecord(ALICE, PLAY_INFO_EPISODE_2),
        state: 'downloading' as const,
        bytes: 300,
      }
      const { manager, files, worker } = managerHarness({
        initial: [recordEntry(downloadedRecord(ALICE)), recordEntry(midFlight)],
      })
      // 9001's file was evicted; 9002 got 500 bytes before the app was killed.
      files.set(NAME_2, 500)
      manager.setOwner(ALICE)

      await manager.hydrate()

      expect(manager.record(9001)).toMatchObject({
        state: 'failed',
        reason: 'evicted',
        message: MESSAGES.evicted,
      })
      // Keep offline again: a fresh download, not a resume into nothing.
      manager.resume(9001)
      expect(worker.lastCommand()).toMatchObject({ name: NAME_1, fresh: true, etag: null })
      expect(manager.record(9002)).toMatchObject({
        state: 'paused',
        reason: 'interrupted',
        bytes: 500,
        message: MESSAGES.interrupted,
      })
    })

    it('calls a whole file downloaded', async () => {
      const { manager, files } = managerHarness({
        initial: [recordEntry({ ...downloadedRecord(ALICE), state: 'downloading' })],
      })
      files.set(NAME_1, 1000)
      manager.setOwner(ALICE)

      await manager.hydrate()

      expect(manager.record(9001)?.state).toBe('downloaded')
    })
  })

  describe('per-user isolation', () => {
    it('shows and plays nothing of another account', async () => {
      const { manager, files } = managerHarness({
        initial: [recordEntry(downloadedRecord(ALICE))],
      })
      files.set(NAME_1, 1000)
      await manager.hydrate()

      manager.setOwner(BOB)
      expect(manager.getSnapshot()).toEqual({})
      expect(manager.isOnDevice(9001)).toBe(false)
      expect(await manager.playableUrl(9001)).toBeNull()

      manager.setOwner(ALICE)
      expect(Object.keys(manager.getSnapshot())).toEqual(['9001'])
    })

    it('keeps a shared file until the last account deletes it', async () => {
      const { manager, files, removed } = managerHarness({
        initial: [recordEntry(downloadedRecord(ALICE)), recordEntry(downloadedRecord(BOB))],
      })
      files.set(NAME_1, 1000)
      await manager.hydrate()

      manager.setOwner(BOB)
      await manager.remove(9001)
      expect(removed).toEqual([])

      manager.setOwner(ALICE)
      expect(manager.isOnDevice(9001)).toBe(true)
      await manager.remove(9001)
      expect(removed).toEqual([NAME_1])
    })

    it("pauses one account's download when another signs in", async () => {
      const { manager, worker } = await started()

      manager.setOwner(BOB)
      expect(worker.lastCommand()).toEqual({ cmd: 'pause', name: NAME_1 })

      manager.setOwner(ALICE)
      expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'interrupted' })
    })
  })

  describe('never touching a file the worker may hold (B1)', () => {
    it('defers the delete of a by-hand pause whose chunk is still in flight', async () => {
      const { manager, worker, removed, files } = await started()
      files.set(NAME_1, 400)
      worker.emit({ type: 'progress', name: NAME_1, offset: 400, total: 1000, etag: '"e"', run: 1 })

      manager.pause(9001)
      await manager.remove(9001)
      // The pause is sent but the worker has not closed the file yet.
      expect(removed).toEqual([])

      worker.emit({ type: 'paused', name: NAME_1, offset: 800, run: 1 })
      expect(removed).toEqual([NAME_1])
    })

    it('deletes then keeps again without the old run touching the new one', async () => {
      const { manager, worker, removed, files } = await started()
      files.set(NAME_1, 400)
      worker.emit({ type: 'progress', name: NAME_1, offset: 400, total: 1000, etag: '"e"', run: 1 })

      await manager.remove(9001)
      await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })

      expect(worker.lastCommand()).toMatchObject({
        cmd: 'download',
        name: NAME_1,
        fresh: true,
        etag: null,
        total: null,
        run: 2,
      })
      // The old run ends: its file is *not* deleted (it is the new download's
      // now) and its numbers do not land on the new record.
      worker.emit({ type: 'progress', name: NAME_1, offset: 800, total: 1000, etag: '"e"', run: 1 })
      worker.emit({ type: 'paused', name: NAME_1, offset: 800, run: 1 })
      expect(removed).toEqual([])
      expect(manager.record(9001)).toMatchObject({ state: 'downloading', bytes: 0 })

      worker.emit({ type: 'progress', name: NAME_1, offset: 10, total: 1000, etag: '"f"', run: 2 })
      expect(manager.record(9001)).toMatchObject({ bytes: 10, etag: '"f"', fresh: false })
    })

    it('starts over, never resumes, from a file no record names', async () => {
      const harness = managerHarness()
      harness.manager.setOwner(ALICE)
      await harness.manager.hydrate()
      harness.files.set(NAME_1, 640)

      await harness.manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })

      expect(harness.manager.record(9001)).toMatchObject({ bytes: 0, fresh: true })
      expect(harness.worker.lastCommand()).toMatchObject({ fresh: true, etag: null })
    })

    it('sweeps, at launch, episode files no record names', async () => {
      const { manager, files, removed } = managerHarness({
        initial: [recordEntry(downloadedRecord(ALICE))],
      })
      files.set(NAME_1, 1000)
      files.set('episode-9999.mp4', 123)

      await manager.hydrate()

      expect(removed).toEqual(['episode-9999.mp4'])
      expect(files.has(NAME_1)).toBe(true)
    })
  })

  it('brings queued downloads with nothing on disk back paused, not gone (S1)', async () => {
    const queued = {
      ...downloadedRecord(ALICE, PLAY_INFO_EPISODE_2),
      state: 'queued' as const,
      bytes: 0,
      total: 0,
    }
    const { manager } = managerHarness({ initial: [recordEntry(queued)] })
    manager.setOwner(ALICE)

    await manager.hydrate()

    expect(manager.record(9002)).toMatchObject({ state: 'paused', reason: 'interrupted' })
  })

  it("pauses another account's queued downloads, not only the running one (S2)", async () => {
    const { manager } = await started([9001, 9002])

    manager.setOwner(BOB)
    manager.setOwner(ALICE)

    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'interrupted' })
    expect(manager.record(9002)).toMatchObject({ state: 'paused', reason: 'interrupted' })
  })

  it('fails the running download with a sentence when the worker dies, and moves on (S5)', async () => {
    const { manager, worker, workers } = await started([9001, 9002])

    worker.fail('Load failed')

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'error' })
    expect(manager.record(9001)?.message).toMatch(/could not run its downloader/)
    expect(worker.terminated).toBe(true)
    // The next send booted a fresh worker for the queue.
    expect(workers).toHaveLength(2)
    expect(workers[1]?.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_2 })
    expect(manager.record(9002)?.state).toBe('downloading')
  })

  describe('sign-out (S6)', () => {
    it('pauses a running download and revokes the live URL', async () => {
      const { manager, worker, revokes } = await started()
      const before = revokes.count

      manager.setOwner(null)

      expect(worker.lastCommand()).toEqual({ cmd: 'pause', name: NAME_1 })
      expect(revokes.count).toBe(before + 1)
      expect(manager.getSnapshot()).toEqual({})
    })

    it('carries on, by itself, a download that failed for want of a session', async () => {
      const { manager, worker } = await started()
      worker.emit({ type: 'failed', name: NAME_1, offset: 0, code: 'auth', reason: 'x', run: 1 })
      manager.setOwner(null)

      manager.setOwner(ALICE)

      expect(manager.record(9001)?.state).toBe('downloading')
      expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_1 })
    })
  })

  it('says another window has it, and tries again when nudged (S10)', async () => {
    const { manager, worker } = await started()

    worker.emit({ type: 'busy', name: NAME_1, run: 1 })
    expect(manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'elsewhere',
      message: MESSAGES.elsewhere,
    })

    manager.nudge()
    expect(manager.record(9001)?.state).toBe('downloading')
    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_1, run: 2 })
  })

  it('turns a file the player would not play into Try again, afresh', async () => {
    const { manager, files, worker } = managerHarness({
      initial: [recordEntry(downloadedRecord(ALICE))],
    })
    files.set(NAME_1, 1000)
    manager.setOwner(ALICE)
    await manager.hydrate()
    expect(await manager.playableUrl(9001)).toBe(`blob:${NAME_1}`)

    manager.markUnplayable(9001)

    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'unreadable' })
    expect(await manager.playableUrl(9001)).toBeNull()
    manager.resume(9001)
    expect(worker.lastCommand()).toMatchObject({ fresh: true })
  })

  it('names the nearest downloaded episodes either side', () => {
    const one = downloadedRecord(ALICE, PLAY_INFO)
    const two = downloadedRecord(ALICE, PLAY_INFO_EPISODE_2)
    const three = downloadedRecord(ALICE, {
      ...PLAY_INFO,
      episode: { ...PLAY_INFO.episode, id: 9003, number: 3 },
    })
    const records = { 9001: one, 9002: { ...two, state: 'paused' as const }, 9003: three }

    expect(downloadedNeighbours(records, one)).toEqual({ previous: null, next: three })
    expect(downloadedNeighbours(records, three)).toEqual({ previous: one, next: null })
  })
})
