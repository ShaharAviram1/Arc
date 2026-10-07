import { describe, expect, it } from 'vitest'
import { BACKGROUNDED, MESSAGES } from '@/offline/downloads'
import { autoKeep } from '@/offline/useTripAutoKeep'
import { PLAY_INFO } from '@/test/animeFixtures'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

/**
 * When the device stops a write (owner incident, 2026-10-07): an iPad that
 * closed the worker's file handle when Arc left the screen, and an iPad whose
 * disk is full — which WebKit reports the same way, `InvalidStateError`, while
 * `estimate()` still shows gigabytes free. The worker re-opens and retries
 * once (download.test.ts); these are what the manager does with what is left.
 */

const ALICE = 1
const NAME_1 = 'episode-9001.mp4'
const NAME_2 = 'episode-9002.mp4'

async function started(options: { visible?: () => boolean } = {}) {
  const harness = managerHarness({ isVisible: options.visible ?? (() => true) })
  harness.manager.setOwner(ALICE)
  await harness.manager.hydrate()
  for (const episodeId of [9001, 9002]) {
    await harness.manager.start({ episodeId, url: `/media/${String(episodeId)}/episode.mp4` })
  }
  return harness
}

const STALE = 'InvalidStateError: failed to write to file'

describe('a write the device stopped', () => {
  it('tries the record once more at once, on a new worker', async () => {
    const { manager, worker, workers } = await started()
    worker.emit({ type: 'progress', name: NAME_1, offset: 400, total: 1000, etag: '"e"', run: 1 })

    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 400,
      reason: 'interrupted',
      detail: STALE,
      run: 1,
    })

    expect(worker.terminated).toBe(true)
    expect(workers).toHaveLength(2)
    expect(manager.record(9001)).toMatchObject({ state: 'downloading', bytes: 400 })
    // The same file, resumed from what it holds, with the validator it had.
    expect(workers[1]?.commands).toEqual([
      expect.objectContaining({ cmd: 'download', name: NAME_1, etag: '"e"', fresh: false }),
    ])
    expect(manager.record(9002)?.state).toBe('queued')
  })

  it('reads a second stop in a row as a full device: paused for space, and the queue held', async () => {
    const { manager, worker, workers } = await started()
    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 0,
      reason: 'interrupted',
      detail: STALE,
      run: 1,
    })
    const second = workers[1]
    second?.emit({
      type: 'paused',
      name: NAME_1,
      offset: 0,
      reason: 'interrupted',
      detail: STALE,
      run: 2,
    })

    expect(manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'quota',
      message: MESSAGES.quota,
    })
    expect(MESSAGES.quota).toMatch(/out of space — free some room and the download continues/)
    // No cascade: the next episode is not handed a first chunk to fail on.
    expect(manager.record(9002)?.state).toBe('queued')
    expect(workers.flatMap((w) => w.commands).some((c) => c.name === NAME_2)).toBe(false)
  })

  it('probes on the next nudge by writing, and lets the queue flow once a chunk lands', async () => {
    const { manager, worker, workers } = await started()
    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 0,
      reason: 'interrupted',
      detail: STALE,
      run: 1,
    })
    workers[1]?.emit({
      type: 'paused',
      name: NAME_1,
      offset: 0,
      reason: 'interrupted',
      detail: STALE,
      run: 2,
    })

    // Still full: the probe fails once and the hold stays, without a second strike.
    manager.nudge()
    expect(manager.record(9001)?.state).toBe('downloading')
    expect(workers).toHaveLength(3)
    workers[2]?.emit({
      type: 'paused',
      name: NAME_1,
      offset: 0,
      reason: 'interrupted',
      detail: STALE,
      run: 3,
    })
    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'quota' })
    expect(manager.record(9002)?.state).toBe('queued')

    // Room was made: the next probe's write lands, and the queue carries on.
    manager.nudge()
    const probe = workers[3]
    expect(probe?.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_1 })
    probe?.emit({ type: 'progress', name: NAME_1, offset: 8, total: 10, etag: '"e"', run: 4 })
    expect(manager.record(9001)?.state).toBe('downloading')
    probe?.emit({ type: 'done', name: NAME_1, offset: 10, total: 10, etag: '"e"', run: 4 })
    expect(manager.record(9001)?.state).toBe('downloaded')
    expect(manager.record(9002)?.state).toBe('downloading')
    expect(probe?.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_2 })
  })

  it('pauses for space at once on a full disk, and probes that record alone', async () => {
    const { manager, worker, workers } = await started()
    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 0,
      reason: 'quota',
      detail: 'QuotaExceededError',
      run: 1,
    })
    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'quota' })
    expect(workers).toHaveLength(1)

    manager.resumeSuspended()
    workers[1]?.emit({ type: 'progress', name: NAME_1, offset: 5, total: 10, etag: null, run: 2 })
    expect(manager.record(9002)?.state).toBe('queued')
  })

  it('off screen: paused as interrupted, the next record runs on a new worker, and the nudge resumes it', async () => {
    let visible = false
    const { manager, worker, workers } = await started({ visible: () => visible })

    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 400,
      reason: 'interrupted',
      detail: STALE,
      run: 1,
    })

    expect(manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'interrupted',
      autoResume: true,
    })
    expect(manager.record(9001)?.message).toBe(`${BACKGROUNDED} (${STALE})`)
    // Never handed to the worker that just said it could not write.
    expect(worker.commands.some((c) => c.cmd === 'download' && c.name === NAME_2)).toBe(false)
    expect(workers[1]?.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_2 })
    expect(worker.terminated).toBe(true)

    // A nudge while still hidden does nothing for it.
    manager.nudge()
    expect(manager.record(9001)?.state).toBe('paused')

    visible = true
    manager.nudge()
    expect(manager.record(9001)?.state).toBe('queued')
    workers[1]?.emit({ type: 'done', name: NAME_2, offset: 10, total: 10, etag: null, run: 2 })
    expect(manager.record(9001)?.state).toBe('downloading')
  })

  it('never resumes a record paused by hand', async () => {
    const { manager, worker } = await started()
    manager.pause(9001)
    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 400,
      reason: 'interrupted',
      detail: STALE,
      run: 1,
    })

    manager.nudge()
    manager.resumeSuspended()
    await autoKeep(null, manager)

    expect(manager.record(9001)).toMatchObject({ state: 'paused', reason: 'by-hand' })
  })

  it('resumes a stopped record on the trip auto-keep tick', async () => {
    let visible = false
    const { manager, worker } = await started({ visible: () => visible })
    worker.emit({
      type: 'paused',
      name: NAME_1,
      offset: 400,
      reason: 'interrupted',
      detail: STALE,
      run: 1,
    })
    expect(manager.record(9001)?.state).toBe('paused')

    visible = true
    await autoKeep(null, manager)

    expect(manager.record(9001)?.state).toMatch(/queued|downloading/)
  })

  it('replaces the worker after a write it could not make, before the next record', async () => {
    const { manager, worker, workers } = await started()

    worker.emit({
      type: 'failed',
      name: NAME_1,
      offset: 0,
      code: 'error',
      reason: 'x',
      write: true,
      run: 1,
    })

    expect(manager.record(9001)?.state).toBe('failed')
    expect(worker.terminated).toBe(true)
    expect(workers[1]?.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_2 })
  })

  it('brings a full disk recorded as a failure back as a pause for space', async () => {
    const stored = {
      ...downloadedRecord(ALICE, PLAY_INFO, 1000),
      state: 'failed' as const,
      reason: 'quota' as const,
      message: 'old words',
      bytes: 400,
    }
    const { manager, files } = managerHarness({ initial: [recordEntry(stored)] })
    files.set(NAME_1, 400)
    manager.setOwner(ALICE)
    await manager.hydrate()

    expect(manager.record(9001)).toMatchObject({
      state: 'paused',
      reason: 'quota',
      message: MESSAGES.quota,
    })
  })

  it('lets go of a file the worker dropped unopened, and runs the delete waiting on it', async () => {
    const { manager, worker, files, removed } = await started()
    files.set(NAME_2, 0)
    // 9001 is paused and 9002 starts: the worker holds 9002 behind 9001.
    manager.pause(9001)
    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: NAME_2 })
    // 9002 is paused before the worker ever opened it, then deleted.
    manager.pause(9002)
    await manager.remove(9002)
    expect(removed).not.toContain(NAME_2)

    worker.emit({ type: 'released', name: NAME_2, run: 2 })

    expect(removed).toContain(NAME_2)
    expect(manager.record(9002)).toBeUndefined()
  })
})
