import { describe, expect, it, vi } from 'vitest'
import { REMOVE_WATCHED_KEY, watchedNote, type DownloadRecord } from '@/offline/downloads'
import { Outbox, type Send } from '@/offline/outbox'
import { memoryStore, type KeyStore } from '@/offline/store'
import { wireWatchedRemoval } from '@/offline/useDownloads'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

/**
 * A watched copy leaves the device by itself (FR-S9, owner 2026-10-08): once
 * the server has accepted the completion, at launch, on a tick, after a
 * flush — never while it is in the player, never when kept, never with the
 * switch off, never while the completion still waits in the outbox.
 */

const ALICE = 1
const BOB = 2

function watched(record: DownloadRecord, patch: Partial<DownloadRecord> = {}): DownloadRecord {
  return { ...record, watched: true, ...patch }
}

async function launch(
  records: DownloadRecord[],
  options: Parameters<typeof managerHarness>[0] = {},
  owner = ALICE,
) {
  const harness = managerHarness({ ...options, initial: records.map(recordEntry) })
  for (const record of records) harness.files.set(record.name, record.bytes)
  harness.manager.setOwner(owner)
  await harness.manager.hydrate()
  return harness
}

describe('removing watched copies', () => {
  it('removes a watched, synced copy at launch', async () => {
    const { manager, removed, store } = await launch([
      watched(downloadedRecord(ALICE)),
      downloadedRecord(ALICE, PLAY_INFO_EPISODE_2),
    ])

    expect(manager.record(9001)).toBeUndefined()
    expect(removed).toEqual(['episode-9001.mp4'])
    expect(await store.get('1:9001')).toBeUndefined()
    // The unwatched one stays.
    expect(manager.record(9002)?.state).toBe('downloaded')
  })

  it('keeps a copy whose completion still waits in the outbox', async () => {
    const queuedFor = vi.fn(() => Promise.resolve(true))
    const { manager, removed } = await launch([watched(downloadedRecord(ALICE))], { queuedFor })

    expect(queuedFor).toHaveBeenCalledWith(ALICE, 9001)
    expect(manager.record(9001)?.state).toBe('downloaded')
    expect(removed).toEqual([])
  })

  it('keeps a copy kept by hand', async () => {
    const { manager, removed } = await launch([watched(downloadedRecord(ALICE), { keep: true })])

    expect(manager.record(9001)?.state).toBe('downloaded')
    expect(removed).toEqual([])
  })

  it('keeps everything with the device switch off', async () => {
    const notes = memoryStore([[REMOVE_WATCHED_KEY, false]])
    const { manager, removed } = await launch([watched(downloadedRecord(ALICE))], { notes })

    expect(manager.getRemoveWatched()).toBe(false)
    expect(manager.record(9001)?.state).toBe('downloaded')
    await manager.removeWatched()
    expect(removed).toEqual([])
  })

  it('waits while the episode is open in the player, and removes it once left', async () => {
    const harness = managerHarness({ initial: [recordEntry(downloadedRecord(ALICE))] })
    harness.files.set('episode-9001.mp4', 1000)
    const { manager, removed } = harness
    manager.setOwner(ALICE)
    await manager.hydrate()
    const leave = manager.enterPlayer(9001)

    // The completion lands mid-session: the server accepted it.
    await manager.noteWatched(ALICE, 9001, true)
    await manager.removeWatched()
    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', watched: true })
    expect(removed).toEqual([])

    leave()
    await manager.removeWatched()
    expect(manager.record(9001)).toBeUndefined()
    expect(removed).toEqual(['episode-9001.mp4'])
  })

  it('holds a copy for as long as any player holds it', async () => {
    const { manager, removed } = await launch([downloadedRecord(ALICE)])
    const first = manager.enterPlayer(9001)
    const second = manager.enterPlayer(9001)
    await manager.noteWatched(ALICE, 9001, true)

    first()
    first() // leaving twice counts once
    await manager.removeWatched()
    expect(removed).toEqual([])

    second()
    await manager.removeWatched()
    expect(removed).toEqual(['episode-9001.mp4'])
  })

  it('sends the trip release call for a trip copy', async () => {
    const releaseDelivered = vi.fn(() => Promise.resolve(null))
    const { manager } = await launch(
      [watched(downloadedRecord(ALICE), { tripId: 77, confirm: 'done' })],
      { releaseDelivered },
    )

    expect(manager.record(9001)).toBeUndefined()
    expect(releaseDelivered).toHaveBeenCalledWith(77, 9001)
    // And the trip does not fetch it again.
    expect(manager.isDeclined(77, 9001)).toBe(true)
  })

  it('marks only the current account’s record, and a full and a small copy stay apart', async () => {
    const mine = {
      ...downloadedRecord(ALICE, PLAY_INFO, 210),
      name: 'episode-9001-o.mp4',
      variant: 'small' as const,
    }
    const theirs = downloadedRecord(BOB, PLAY_INFO, 700)
    const { manager, removed, store } = await launch([mine, theirs])

    await manager.noteWatched(ALICE, 9001, true)
    await manager.removeWatched()

    expect(removed).toEqual(['episode-9001-o.mp4'])
    expect(await store.get('2:9001')).toMatchObject({ state: 'downloaded' })
    expect((await store.get<DownloadRecord>('2:9001'))?.watched).toBeUndefined()
  })

  it('leaves another account’s watched copy alone while somebody else is signed in', async () => {
    const { removed } = await launch([watched(downloadedRecord(BOB))], {}, ALICE)
    expect(removed).toEqual([])
  })

  it('un-marks before the pass: the copy stays', async () => {
    const { manager, removed } = await launch([downloadedRecord(ALICE)])
    const leave = manager.enterPlayer(9001)
    await manager.noteWatched(ALICE, 9001, true)
    await manager.noteWatched(ALICE, 9001, false)
    leave()
    await manager.removeWatched()

    expect(manager.record(9001)).toMatchObject({ state: 'downloaded', watched: false })
    expect(removed).toEqual([])
  })

  it('takes Keep on and off per episode', async () => {
    const { manager, store } = await launch([downloadedRecord(ALICE)])
    manager.setKeep(9001, true)
    expect(manager.record(9001)?.keep).toBe(true)
    expect(await store.get('1:9001')).toMatchObject({ keep: true })
    manager.setKeep(9001, false)
    expect(manager.record(9001)?.keep).toBe(false)
  })

  it('remembers the switch across a reload', async () => {
    const notes: KeyStore = memoryStore()
    const first = await launch([], { notes })
    expect(first.manager.getRemoveWatched()).toBe(true)
    first.manager.setRemoveWatched(false)
    await Promise.resolve()

    const second = await launch([], { notes })
    expect(second.manager.getRemoveWatched()).toBe(false)
    second.manager.setRemoveWatched(true)
    await Promise.resolve()

    const third = await launch([], { notes })
    expect(third.manager.getRemoveWatched()).toBe(true)
  })
})

describe('watchedNote', () => {
  const record = downloadedRecord(ALICE)
  it('says what a row is waiting for', () => {
    expect(watchedNote(record, true)).toBeNull()
    expect(watchedNote({ ...record, watched: true }, true)).toBe('Watched · removing soon')
    expect(watchedNote({ ...record, watched: true, keep: true }, true)).toBe('Kept')
    expect(watchedNote({ ...record, keep: true }, true)).toBe('Kept')
    expect(watchedNote({ ...record, watched: true }, false)).toBeNull()
  })
})

describe('the outbox tells the manager (wireWatchedRemoval)', () => {
  /** An outbox whose server answers every item with `status` while `net.up`, and nothing otherwise. */
  function outboxAnswering(status: 'applied' | 'stale', net = { up: true }) {
    const send = vi.fn<Send>((body) =>
      net.up
        ? Promise.resolve({
            results: body.items.map((item) => ({
              client_id: item.client_id,
              status,
              reason: null,
            })),
          })
        : Promise.reject(new TypeError('Load failed')),
    )
    let n = 0
    const box = new Outbox({
      store: memoryStore(),
      send,
      newId: () => `id-${String((n += 1))}`,
      persistent: () => Promise.resolve(true),
      locks: null,
    })
    box.setOwner(ALICE)
    return box
  }

  it('a completion queued offline keeps the copy; once replayed and applied, it goes', async () => {
    const net = { up: false }
    const box = outboxAnswering('applied', net)
    const { manager, removed } = await launch([downloadedRecord(ALICE)], {
      queuedFor: async (userId, episodeId) =>
        (await box.all()).some(
          (record) => record.user_id === userId && record.episode_id === episodeId,
        ),
    })
    const unwire = wireWatchedRemoval(box, manager)

    await box.recordCompletion(ALICE, 9001)
    await box.flush() // no network
    await manager.removeWatched()
    expect(manager.record(9001)?.state).toBe('downloaded')
    expect(manager.record(9001)?.watched).toBeUndefined()

    // Back online: the flush lands, the server applies the completion.
    net.up = true
    await box.flush()
    await vi.waitFor(() => {
      expect(removed).toEqual(['episode-9001.mp4'])
    })
    unwire()
  })

  it('a stale completion (un-marked later elsewhere) removes nothing', async () => {
    const box = outboxAnswering('stale')
    const { manager, removed } = await launch([downloadedRecord(ALICE)])
    const unwire = wireWatchedRemoval(box, manager)

    await box.recordCompletion(ALICE, 9001)
    await box.flush()
    await manager.removeWatched()

    expect(manager.record(9001)?.watched).toBeUndefined()
    expect(removed).toEqual([])
    unwire()
  })

  it('an un-mark queued offline clears the mark at once', async () => {
    const box = outboxAnswering('applied', { up: false })
    const { manager } = await launch([downloadedRecord(ALICE)])
    const unwire = wireWatchedRemoval(box, manager)
    const leave = manager.enterPlayer(9001)
    box.announceWatched(ALICE, 9001, true)
    await manager.noteWatched(ALICE, 9001, true)

    await box.recordUnmark(ALICE, 9001)
    await vi.waitFor(() => {
      expect(manager.record(9001)?.watched).toBe(false)
    })
    leave()
    unwire()
  })
})
