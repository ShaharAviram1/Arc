import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import {
  noteServerPosition,
  recallPosition,
  recallServerPosition,
  rememberPosition,
  rememberUser,
} from '@/offline/cache'
import { setDownloads, type DownloadRecord } from '@/offline/downloads'
import { resetNetwork } from '@/offline/network'
import { Outbox, setOutbox } from '@/offline/outbox'
import { memoryStore } from '@/offline/store'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { mockApi, TEST_DEMO_USER, TEST_USER } from '@/test/apiMock'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'
import { instances, resetHls } from '@/test/hlsMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

const BLOB = 'blob:episode-9001.mp4'

function install(records: DownloadRecord[], blobUrl?: (name: string) => Promise<string | null>) {
  const harness = managerHarness({
    initial: records.map(recordEntry),
    ...(blobUrl === undefined ? {} : { blobUrl }),
  })
  for (const record of records) harness.files.set(record.name, record.bytes)
  setDownloads(harness.manager)
  return harness
}

function renderPlayer(at: number | string = 9001) {
  const path = typeof at === 'number' ? `/watch/${String(at)}` : at
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

function noNetwork() {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.reject(new TypeError('Load failed'))),
  )
}

/** jsdom's media element is inert; give it a playhead and a play() that works. */
function equip(video: HTMLVideoElement, duration = 1436.8): void {
  let currentTime = 0
  let paused = true
  Object.defineProperties(video, {
    currentTime: {
      configurable: true,
      get: () => currentTime,
      set: (value: number) => {
        currentTime = value
      },
    },
    duration: { configurable: true, get: () => duration },
    paused: { configurable: true, get: () => paused },
    play: {
      configurable: true,
      value: () => {
        paused = false
        fireEvent.play(video)
        return Promise.resolve()
      },
    },
    pause: {
      configurable: true,
      value: () => {
        paused = true
        fireEvent.pause(video)
      },
    },
  })
}

async function deviceVideo(duration = 1436.8): Promise<HTMLVideoElement> {
  const video = await waitFor(() => {
    const found = document.querySelector('video')
    if (found === null || found.getAttribute('src') === null) throw new Error('no source yet')
    return found
  })
  equip(video, duration)
  return video
}

/** A browser that can keep episodes: OPFS and a Worker constructor (jsdom has neither). */
function pretendOpfs() {
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: { getDirectory: () => Promise.reject(new Error('not in tests')) },
  })
  vi.stubGlobal('Worker', class {})
}

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'storage')
  setDownloads(null)
  resetHls()
  setOutbox(null)
  resetNetwork()
})

describe('the player and a downloaded episode (FR-S9)', () => {
  it('plays the file on the device even when online, without hls.js', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/episodes/9001/play': { body: PLAY_INFO },
      'POST /api/progress': {
        body: { completed: false, newly_completed: false, list_progress: null },
      },
    })
    pretendOpfs()
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()

    const video = await deviceVideo()
    expect(video.getAttribute('src')).toBe(BLOB)
    expect(instances).toHaveLength(0)
    // The offline control says it, in place of the old "· on this device" suffix.
    expect(
      await screen.findByRole('button', { name: 'Episode 1 is on this device' }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/· on this device/)).not.toBeInTheDocument()

    // The server's resume still applies online.
    await act(async () => {
      fireEvent.loadedMetadata(video)
      await Promise.resolve()
    })
    expect(video.currentTime).toBe(PLAY_INFO.resume_position)
  })

  it('launched with no network, plays it and resumes from the position on the device', async () => {
    await rememberUser(TEST_USER)
    await rememberPosition(TEST_USER.id, 9001, 312, 1436.8)
    install([downloadedRecord(TEST_USER.id), downloadedRecord(TEST_USER.id, PLAY_INFO_EPISODE_2)])
    noNetwork()

    renderPlayer()

    const video = await deviceVideo()
    expect(video.getAttribute('src')).toBe(BLOB)
    expect(
      screen.getByRole('heading', { name: PLAY_INFO.anime.title.preferred }),
    ).toBeInTheDocument()
    await act(async () => {
      fireEvent.loadedMetadata(video)
      await Promise.resolve()
    })
    expect(video.currentTime).toBe(312)
    expect(await screen.findByText('Resumed from 5:12')).toBeInTheDocument()
    // Next is the next *downloaded* episode, which is the one that can play.
    expect(screen.getByRole('link', { name: 'Next episode 2' })).toHaveAttribute(
      'href',
      '/watch/9002',
    )
  })

  it('keeps offline progress in the outbox and the position on the device', async () => {
    const box = new Outbox({
      store: memoryStore(),
      persistent: () => Promise.resolve(true),
      locks: null,
    })
    setOutbox(box)
    await rememberUser(TEST_USER)
    install([downloadedRecord(TEST_USER.id)])
    noNetwork()

    renderPlayer()
    const video = await deviceVideo()
    await act(async () => {
      fireEvent.loadedMetadata(video)
      await Promise.resolve()
    })

    video.currentTime = 600
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)

    await waitFor(async () => {
      expect((await box.all()).map((record) => [record.kind, record.position_s])).toEqual([
        ['position', 600],
      ])
    })
    await waitFor(async () => {
      expect(await recallPosition(TEST_USER.id, 9001)).toMatchObject({ position_s: 600 })
    })
  })

  it('offline, says so for an episode that is not downloaded and points to Downloads', async () => {
    await rememberUser(TEST_USER)
    install([])
    noNetwork()

    renderPlayer()

    expect(await screen.findByRole('heading', { name: 'You’re offline' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to Downloads' })).toHaveAttribute(
      'href',
      '/downloads',
    )
  })

  it('falls back to the stream when the file will not play and the server is there', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/episodes/9001/play': { body: PLAY_INFO },
    })
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    const video = await deviceVideo()
    fireEvent.error(video)

    await waitFor(() => {
      expect(instances).toHaveLength(1)
    })
    expect(instances[0]?.loadSource).toHaveBeenCalledWith(PLAY_INFO.playlist_url)
  })

  it('marks a file the element will not play as failed, and says so offline', async () => {
    await rememberUser(TEST_USER)
    const { manager } = install([downloadedRecord(TEST_USER.id)])
    noNetwork()

    renderPlayer()
    const video = await deviceVideo()
    await act(async () => {
      fireEvent.loadedMetadata(video)
      await Promise.resolve()
    })
    // Mid-playback, the file goes bad.
    video.currentTime = 400
    fireEvent.timeUpdate(video)
    fireEvent.error(video)

    expect(await screen.findByText(/could not be read/)).toBeInTheDocument()
    expect(manager.record(9001)).toMatchObject({ state: 'failed', reason: 'unreadable' })
  })

  it('moves to the next downloaded episode, minting its URL only once the old video is gone', async () => {
    await rememberUser(TEST_USER)
    const heldAtMint: (string | null)[] = []
    install(
      [downloadedRecord(TEST_USER.id), downloadedRecord(TEST_USER.id, PLAY_INFO_EPISODE_2)],
      (name) => {
        // What is still on a <video> when a new URL is minted (and, with the
        // real store, the previous one revoked).
        heldAtMint.push(document.querySelector('video')?.getAttribute('src') ?? null)
        return Promise.resolve(`blob:${name}`)
      },
    )
    noNetwork()

    renderPlayer()
    await deviceVideo()
    fireEvent.click(screen.getByRole('link', { name: 'Next episode 2' }))

    await waitFor(() => {
      expect(document.querySelector('video')?.getAttribute('src')).toBe('blob:episode-9002.mp4')
    })
    expect(heldAtMint).toEqual([null, null])
  })

  describe('a fragmented MP4 that does not know its own length (S11)', () => {
    it('resumes against the remembered duration instead', async () => {
      await rememberUser(TEST_USER)
      await rememberPosition(TEST_USER.id, 9001, 312, 1436.8)
      install([downloadedRecord(TEST_USER.id)])
      noNetwork()

      renderPlayer()
      const video = await deviceVideo(Number.POSITIVE_INFINITY)
      await act(async () => {
        fireEvent.loadedMetadata(video)
        await Promise.resolve()
      })

      expect(video.currentTime).toBe(312)
      expect(screen.getByRole('slider', { name: 'Seek' })).toHaveAttribute('aria-valuemax', '1437')
    })

    it('does not seek at all when no length is known anywhere', async () => {
      await rememberUser(TEST_USER)
      await rememberPosition(TEST_USER.id, 9001, 312, 0)
      const record = downloadedRecord(TEST_USER.id)
      install([{ ...record, snapshot: { ...record.snapshot, duration: 0 } }])
      noNetwork()

      renderPlayer()
      const video = await deviceVideo(Number.NaN)
      await act(async () => {
        fireEvent.loadedMetadata(video)
        await Promise.resolve()
      })

      expect(video.currentTime).toBe(0)
      expect(screen.queryByText(/Resumed from/)).not.toBeInTheDocument()
    })
  })
})

describe('resume from the newer position, online or offline (FR-S2, 2026-10-08)', () => {
  const EARLIER = new Date('2026-10-08T08:00:00Z')
  const LATER = new Date('2026-10-08T09:00:00Z')
  const ONLINE = {
    'GET /api/auth/me': { body: TEST_USER },
    'GET /api/episodes/9001/play': { body: PLAY_INFO },
    'POST /api/progress': {
      body: { completed: false, newly_completed: false, list_progress: null },
    },
  }

  beforeEach(() => {
    // A clean outbox: one left from another test would hold this episode's
    // progress as still waiting, which is a rule of its own.
    setOutbox(
      new Outbox({ store: memoryStore(), persistent: () => Promise.resolve(true), locks: null }),
    )
  })

  async function opened(video: HTMLVideoElement): Promise<void> {
    await act(async () => {
      fireEvent.loadedMetadata(video)
      await Promise.resolve()
    })
  }

  /** The stream's element: hls.js attaches it, so it never gets a `src`. */
  async function streamVideo(): Promise<HTMLVideoElement> {
    const video = await waitFor(() => {
      const found = document.querySelector('video')
      if (found === null) throw new Error('no video yet')
      return found
    })
    equip(video)
    return video
  }

  it("online, compares with the server's own timestamp: the device written later wins", async () => {
    await rememberPosition(TEST_USER.id, 9001, 1000, 1436.8, LATER)
    mockApi({
      ...ONLINE,
      'GET /api/episodes/9001/play': {
        body: { ...PLAY_INFO, resume_at: EARLIER.toISOString() },
      },
    })
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    const video = await deviceVideo()
    await opened(video)

    expect(video.currentTime).toBe(1000)
    expect(await screen.findByText('Resumed from 16:40')).toBeInTheDocument()
  })

  it("online, compares with the server's own timestamp: the server written later wins", async () => {
    await rememberPosition(TEST_USER.id, 9001, 1000, 1436.8, EARLIER)
    mockApi({
      ...ONLINE,
      'GET /api/episodes/9001/play': { body: { ...PLAY_INFO, resume_at: LATER.toISOString() } },
    })
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    const video = await deviceVideo()
    await opened(video)

    expect(video.currentTime).toBe(754)
    expect(await screen.findByText('Resumed from 12:34')).toBeInTheDocument()
  })

  it('online, plays the device copy from the device position when it is newer', async () => {
    // The server holds 12:34 — what this iPad saw it hold before it went on
    // watching with no network, up to 16:40.
    await noteServerPosition(TEST_USER.id, 9001, 754, 1436.8, EARLIER)
    await rememberPosition(TEST_USER.id, 9001, 1000, 1436.8, LATER)
    mockApi(ONLINE)
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    const video = await deviceVideo()
    await opened(video)

    expect(video.currentTime).toBe(1000)
    expect(await screen.findByText('Resumed from 16:40')).toBeInTheDocument()
  })

  it('online, takes the server position when another device moved it', async () => {
    await noteServerPosition(TEST_USER.id, 9001, 300, 1436.8, EARLIER)
    await rememberPosition(TEST_USER.id, 9001, 400, 1436.8, LATER)
    mockApi(ONLINE)
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    const video = await deviceVideo()
    await opened(video)

    expect(video.currentTime).toBe(754)
    expect(await screen.findByText('Resumed from 12:34')).toBeInTheDocument()
    // And the server's position is what this device has now seen it hold.
    expect(await recallServerPosition(TEST_USER.id, 9001)).toMatchObject({ position_s: 754 })
  })

  it('online, a streamed episode resumes from the device too when it is newer', async () => {
    await noteServerPosition(TEST_USER.id, 9001, 754, 1436.8, EARLIER)
    await rememberPosition(TEST_USER.id, 9001, 900, 1436.8, LATER)
    mockApi(ONLINE)
    install([])

    renderPlayer()
    const video = await streamVideo()
    await opened(video)

    expect(video.currentTime).toBe(900)
    expect(await screen.findByText('Resumed from 15:00')).toBeInTheDocument()
  })

  it('offline, resumes from the device position', async () => {
    await rememberUser(TEST_USER)
    await noteServerPosition(TEST_USER.id, 9001, 900, 1436.8, LATER)
    await rememberPosition(TEST_USER.id, 9001, 488, 1436.8, EARLIER)
    install([downloadedRecord(TEST_USER.id)])
    noNetwork()

    renderPlayer()
    const video = await deviceVideo()
    await opened(video)

    expect(video.currentTime).toBe(488)
    expect(await screen.findByText('Resumed from 8:08')).toBeInTheDocument()
  })

  it('does not resume an episode the device finished, whatever the server held', async () => {
    await noteServerPosition(TEST_USER.id, 9001, 754, 1436.8, EARLIER)
    await rememberPosition(TEST_USER.id, 9001, 1430, 1436.8, LATER)
    mockApi(ONLINE)
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    const video = await deviceVideo()
    await opened(video)

    expect(video.currentTime).toBe(0)
    expect(screen.queryByText(/Resumed from/)).not.toBeInTheDocument()
  })

  it('a trip-only device copy with a finite length resumes, though the server knows none', async () => {
    await noteServerPosition(TEST_USER.id, 9001, null, 0, EARLIER)
    await rememberPosition(TEST_USER.id, 9001, 488, 1470, LATER)
    const tripOnly = {
      ...PLAY_INFO,
      duration: 0,
      offline_only: true,
      playlist_url: null,
      resume_position: null,
    }
    mockApi({ ...ONLINE, 'GET /api/episodes/9001/play': { body: tripOnly } })
    const record = downloadedRecord(TEST_USER.id, tripOnly)
    install([record])

    renderPlayer()
    const video = await deviceVideo(1470)
    await opened(video)

    expect(video.currentTime).toBe(488)
    expect(await screen.findByText('Resumed from 8:08')).toBeInTheDocument()
  })

  it('writes the device position while streaming, not only from the device copy', async () => {
    mockApi(ONLINE)
    install([])

    renderPlayer()
    const video = await streamVideo()
    await opened(video)
    video.currentTime = 600
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)

    await waitFor(async () => {
      expect(await recallPosition(TEST_USER.id, 9001)).toMatchObject({
        position_s: 600,
        updated_at: expect.any(String) as unknown,
      })
    })
    // The report the server took is what it now holds.
    await waitFor(async () => {
      expect(await recallServerPosition(TEST_USER.id, 9001)).toMatchObject({ position_s: 600 })
    })
  })

  it("Downloads' Play opens the episode at the device position", async () => {
    await rememberUser(TEST_USER)
    await rememberPosition(TEST_USER.id, 9001, 488, 1436.8)
    install([downloadedRecord(TEST_USER.id)])
    noNetwork()

    renderPlayer('/downloads')
    fireEvent.click(await screen.findByRole('link', { name: /^Play / }))
    const video = await deviceVideo()
    await opened(video)

    expect(video.currentTime).toBe(488)
    expect(await screen.findByText('Resumed from 8:08')).toBeInTheDocument()
  })
})

describe('the offline control in the player (FR-S9, owner 2026-10-05)', () => {
  const STREAMING = {
    'GET /api/auth/me': { body: TEST_USER },
    'GET /api/episodes/9001/play': { body: PLAY_INFO },
    'POST /api/progress': {
      body: { completed: false, newly_completed: false, list_progress: null },
    },
  }

  it('downloads the episode being streamed without swapping the source mid-playback', async () => {
    pretendOpfs()
    mockApi(STREAMING)
    const { manager, worker } = install([])

    renderPlayer()
    await waitFor(() => {
      expect(instances).toHaveLength(1)
    })
    const video = document.querySelector('video') as HTMLVideoElement
    equip(video)
    await act(async () => {
      fireEvent.loadedMetadata(video)
      await Promise.resolve()
    })
    video.currentTime = 800
    fireEvent.timeUpdate(video)

    fireEvent.click(await screen.findByRole('button', { name: 'Keep episode 1 offline' }))
    await waitFor(() => {
      expect(worker.lastCommand()).toMatchObject({ cmd: 'download' })
    })
    act(() => {
      worker.emit({
        type: 'progress',
        name: 'episode-9001.mp4',
        offset: 50,
        total: 200,
        etag: null,
      })
    })
    expect(
      screen.getByRole('button', { name: 'Downloading episode 1, 25% — pause' }),
    ).toBeInTheDocument()
    act(() => {
      worker.emit({ type: 'done', name: 'episode-9001.mp4', offset: 200, total: 200, etag: '"e"' })
    })

    expect(
      await screen.findByRole('button', { name: 'Episode 1 is on this device' }),
    ).toBeInTheDocument()
    expect(manager.isOnDevice(9001)).toBe(true)
    // Still the same stream, on the same element, at the same place: the
    // device copy is for the next time the episode is opened.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    expect(instances).toHaveLength(1)
    expect(instances[0]?.destroy).not.toHaveBeenCalled()
    expect(document.querySelector('video')).toBe(video)
    expect(video.getAttribute('src')).toBeNull()
    expect(video.currentTime).toBe(800)

    // Streaming, so the new copy can be removed again at once.
    fireEvent.click(screen.getByRole('button', { name: 'Episode 1 is on this device' }))
    expect(screen.getByRole('button', { name: 'Remove from this device' })).toBeEnabled()
  })

  it('will not remove the copy it is playing from, and says why', async () => {
    pretendOpfs()
    mockApi(STREAMING)
    const { manager } = install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    await deviceVideo()
    fireEvent.click(await screen.findByRole('button', { name: 'Episode 1 is on this device' }))

    const remove = screen.getByRole('button', { name: 'Remove from this device' })
    expect(remove).toBeDisabled()
    expect(screen.getByText(/Playing from this copy/)).toBeInTheDocument()
    fireEvent.click(remove)
    expect(manager.record(9001)?.state).toBe('downloaded')
  })

  it('keeps the chrome up while its menu is open', async () => {
    pretendOpfs()
    mockApi(STREAMING)
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    await deviceVideo()
    const button = await screen.findByRole('button', { name: 'Episode 1 is on this device' })
    vi.useFakeTimers()
    try {
      fireEvent.click(button)
      // A sign of life restarts the idle timer, now on the fake clock.
      fireEvent.pointerMove(window)
      act(() => {
        vi.advanceTimersByTime(5000)
      })
      expect(screen.getByRole('button', { name: 'Remove from this device' })).toBeVisible()
      expect(
        screen.getByRole('group', { name: 'Player controls', hidden: true }).parentElement,
      ).toHaveAttribute('aria-hidden', 'false')
    } finally {
      vi.useRealTimers()
    }
  })

  it('is not offered to the demo account', async () => {
    pretendOpfs()
    mockApi({ ...STREAMING, 'GET /api/auth/me': { body: TEST_DEMO_USER } })
    install([])

    renderPlayer()
    await waitFor(() => {
      expect(instances).toHaveLength(1)
    })
    expect(screen.getByRole('link', { name: 'Back to show' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /offline|on this device/ })).not.toBeInTheDocument()
  })
})

describe('a watched copy and the player (owner, 2026-10-08)', () => {
  it('is not removed while its episode is open, and goes once the player is left', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/episodes/9001/play': { body: PLAY_INFO },
      'POST /api/progress': {
        body: { completed: true, newly_completed: true, list_progress: 1 },
      },
    })
    pretendOpfs()
    const { manager, removed } = install([downloadedRecord(TEST_USER.id)])

    renderPlayer()
    await deviceVideo()
    // The server accepts the completion mid-session.
    await manager.noteWatched(TEST_USER.id, 9001, true)
    await manager.removeWatched()
    expect(removed).toEqual([])
    expect(manager.record(9001)?.watched).toBe(true)

    cleanup()
    await manager.removeWatched()
    expect(removed).toEqual(['episode-9001.mp4'])
  })
})
