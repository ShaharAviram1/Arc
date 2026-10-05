import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import {
  loadPlayInfo,
  offlinePlayInfo,
  sendProgress,
  type PlayInfo,
  type PlayInfoDeps,
} from '@/lib/playback'
import { Outbox } from '@/offline/outbox'
import { memoryStore } from '@/offline/store'
import { chooseSource, useLocalCopy } from '@/player/source'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

const USER = 1
const NAME_1 = 'episode-9001.mp4'

async function downloadedManager(records = [downloadedRecord(USER)]) {
  const harness = managerHarness({ initial: records.map(recordEntry) })
  for (const record of records) harness.files.set(record.name, record.bytes)
  harness.manager.setOwner(USER)
  await harness.manager.hydrate()
  return harness
}

function deps(overrides: Partial<PlayInfoDeps> & Pick<PlayInfoDeps, 'manager'>): PlayInfoDeps {
  return {
    fetch: () => Promise.reject(new TypeError('Load failed')),
    recall: () => Promise.resolve(null),
    pending: () => new Map(),
    ...overrides,
  }
}

describe('chooseSource', () => {
  it('plays the file on the device whenever there is one, online or not', () => {
    expect(chooseSource(PLAY_INFO, 'blob:x')).toEqual({ kind: 'file', url: 'blob:x' })
  })

  it('streams an episode that is not downloaded', () => {
    expect(chooseSource(PLAY_INFO, null)).toEqual({
      kind: 'stream',
      url: PLAY_INFO.playlist_url,
    })
  })

  it('has nothing to play for a rebuilt payload whose file would not open', () => {
    const offline: PlayInfo = { ...PLAY_INFO, playlist_url: '', from_device: true }
    expect(chooseSource(offline, null)).toEqual({ kind: 'none' })
  })
})

describe('loadPlayInfo', () => {
  it('is the server answer when there is one', async () => {
    const { manager } = await downloadedManager()
    const info = await loadPlayInfo(
      9001,
      deps({ manager, fetch: () => Promise.resolve(PLAY_INFO) }),
    )
    expect(info).toBe(PLAY_INFO)
  })

  it('offline, rebuilds a downloaded episode and resumes from the position on the device', async () => {
    const { manager } = await downloadedManager([
      downloadedRecord(USER, PLAY_INFO),
      downloadedRecord(USER, PLAY_INFO_EPISODE_2),
    ])
    const recall = vi.fn(() =>
      Promise.resolve({ position_s: 312, duration_s: 1436.8, at: '2026-10-05T10:00:00Z' }),
    )

    const info = await loadPlayInfo(9001, deps({ manager, recall }))

    expect(recall).toHaveBeenCalledWith(USER, 9001)
    expect(info).toMatchObject({
      from_device: true,
      playlist_url: '',
      resume_position: 312,
      duration: PLAY_INFO.duration,
      previous: null,
      next: { id: 9002, number: 2, ready: true },
    })
    expect(info.anime.title.preferred).toBe(PLAY_INFO.anime.title.preferred)
  })

  it('offline, with nothing watched on the device, starts from the beginning', async () => {
    const { manager } = await downloadedManager()
    const info = await loadPlayInfo(9001, deps({ manager }))
    expect(info.resume_position).toBeNull()
  })

  it('plays the download when retention has removed the server copy (404)', async () => {
    const { manager } = await downloadedManager()
    const info = await loadPlayInfo(
      9001,
      deps({ manager, fetch: () => Promise.reject(new ApiError(404, { detail: 'gone' })) }),
    )
    expect(info.from_device).toBe(true)
  })

  it('fails as before for an episode that is not downloaded', async () => {
    const { manager } = await downloadedManager([])
    await expect(loadPlayInfo(9001, deps({ manager }))).rejects.toThrow('Load failed')
  })

  it('fails as before on a server error, downloaded or not', async () => {
    const { manager } = await downloadedManager()
    await expect(
      loadPlayInfo(9001, deps({ manager, fetch: () => Promise.reject(new ApiError(500, null)) })),
    ).rejects.toBeInstanceOf(ApiError)
  })

  it("never rebuilds another account's download", async () => {
    const { manager } = await downloadedManager()
    manager.setOwner(2)
    await expect(loadPlayInfo(9001, deps({ manager }))).rejects.toThrow('Load failed')
  })
})

describe('offlinePlayInfo', () => {
  it('shows a mark still waiting in the outbox', () => {
    const record = downloadedRecord(USER)
    const info = offlinePlayInfo(record, { 9001: record }, null, new Map([[9001, true]]))
    expect(info.episode).toMatchObject({ watched: true, watched_source: 'arc' })
  })
})

describe('progress from a downloaded episode, offline', () => {
  it('goes to the outbox, exactly as streaming progress does (FR-S8)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Load failed'))),
    )
    const box = new Outbox({
      store: memoryStore(),
      persistent: () => Promise.resolve(true),
      locks: null,
    })
    box.setOwner(USER)

    const result = await sendProgress(
      { episode_id: 9001, position_s: 1300, duration_s: 1436.8 },
      box,
    )

    expect(result.queued).toBe(true)
    const kinds = (await box.all()).map((record) => record.kind).sort()
    // Past the 90 % mark: the completion is its own record (FR-S8 rule 1).
    expect(kinds).toEqual(['completion', 'position'])
  })
})

describe('useLocalCopy', () => {
  it('resolves the blob URL before the player mounts the video', async () => {
    const { manager } = await downloadedManager()
    const { result } = renderHook(() => useLocalCopy(9001, manager))

    expect(result.current.checked).toBe(false)
    await waitFor(() => {
      expect(result.current.checked).toBe(true)
    })
    expect(result.current.url).toBe(`blob:${NAME_1}`)
  })

  it('answers at once, with no wait, for a URL already minted', async () => {
    const { manager } = await downloadedManager()
    await manager.playableUrl(9001)
    const { result } = renderHook(() => useLocalCopy(9001, manager))
    expect(result.current).toMatchObject({ checked: true, url: `blob:${NAME_1}` })
  })

  it('checks and finds nothing for an episode that is not downloaded', async () => {
    const { manager } = await downloadedManager([])
    const { result } = renderHook(() => useLocalCopy(9001, manager))
    await waitFor(() => {
      expect(result.current.checked).toBe(true)
    })
    expect(result.current.url).toBeNull()
  })
})
