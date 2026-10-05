import { QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import { recallPosition, rememberPosition, rememberUser } from '@/offline/cache'
import { setDownloads, type DownloadRecord } from '@/offline/downloads'
import { resetNetwork } from '@/offline/network'
import { Outbox, setOutbox } from '@/offline/outbox'
import { memoryStore } from '@/offline/store'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { mockApi, TEST_USER } from '@/test/apiMock'
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

function renderPlayer(episodeId = 9001) {
  const router = createMemoryRouter(routes, { initialEntries: [`/watch/${String(episodeId)}`] })
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

afterEach(() => {
  vi.unstubAllGlobals()
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
    install([downloadedRecord(TEST_USER.id)])

    renderPlayer()

    const video = await deviceVideo()
    expect(video.getAttribute('src')).toBe(BLOB)
    expect(instances).toHaveLength(0)
    expect(screen.getByText(/on this device/)).toBeInTheDocument()

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
