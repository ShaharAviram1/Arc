import { QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { routes as appRoutes } from '@/app/router'
import { LIVE_POLL_MS, type PlayInfo } from '@/lib/playback'
import { createQueryClient } from '@/lib/queryClient'
import { Player } from '@/pages/Player'
import { setDownloads } from '@/offline/downloads'
import { PLAY_INFO } from '@/test/animeFixtures'
import { jsonBodyOf, mockApi, requestsMade, TEST_USER, type MockRoutes } from '@/test/apiMock'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'
import { instances, resetHls } from '@/test/hlsMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

/**
 * Playing while the episode downloads, on the client (FR-P8, FR-S2, FR-S4,
 * FR-S6; architecture §5.4a; M20 T7): the duration is the server's, the
 * scrubber shows the download front, a seek past the front waits and then
 * jumps, the keys are held to the front, `/play` is polled every 5 s for the
 * numbers until the download ends or the episode is adopted, and the few-
 * seeders line.
 */

const EPISODE_ID = 9001
const PLAY = `GET /api/episodes/${String(EPISODE_ID)}/play`
const EXPECTED = 1440

function liveInfo(overrides: Partial<NonNullable<PlayInfo['live']>> = {}): PlayInfo {
  return {
    ...PLAY_INFO,
    resume_position: null,
    duration: EXPECTED,
    subtitles: null,
    live: {
      front_seconds: 300,
      expected_duration: EXPECTED,
      download_progress: 0.43,
      seeders: 12,
      ended: false,
      ...overrides,
    },
  }
}

/**
 * Renders the player. `routes` is read on every request, so a test changes
 * an answer by assigning to it. `POST /api/progress` answers as the server
 * would: completed at 90 % of the duration the client sent.
 */
function renderPlayer(routes: MockRoutes, withAppRoutes = false) {
  const base = mockApi({ 'GET /api/auth/me': { body: TEST_USER }, ...routes })
  const fetchMock = vi.fn((input: string | URL | Request, init?: RequestInit) => {
    const play = routes[PLAY]
    const path = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if ((init?.method ?? 'GET') === 'GET' && path === PLAY.slice(4) && play !== undefined) {
      return Promise.resolve(
        new Response(JSON.stringify(play.body), {
          status: play.status ?? 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    }
    if (init?.method === 'POST' && path === '/api/progress') {
      const sent = jsonBodyOf(init)
      const done = Number(sent.position_s) / Number(sent.duration_s) >= 0.9
      return Promise.resolve(
        new Response(
          JSON.stringify({ completed: done, newly_completed: done, list_progress: null }),
          { headers: { 'Content-Type': 'application/json' } },
        ),
      )
    }
    return base(input, init)
  })
  vi.stubGlobal('fetch', fetchMock)

  // The app's own routes when a test needs its shell (the device's downloads
  // are loaded for the signed-in account there).
  const router = createMemoryRouter(
    withAppRoutes
      ? appRoutes
      : [
          { path: '/watch/:episodeId', element: <Player /> },
          { path: '/anime/:id', element: <p>show page</p> },
          { path: '/', element: <p>home</p> },
        ],
    { initialEntries: [`/watch/${String(EPISODE_ID)}`] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { fetchMock }
}

function polls(fetchMock: ReturnType<typeof vi.fn>): number {
  return requestsMade(fetchMock as Parameters<typeof requestsMade>[0]).filter(
    (made) => made === PLAY,
  ).length
}

/**
 * The element as a growing stream leaves it: `duration` is only what has
 * downloaded (or Infinity on Safari), and `seekable` ends at the front, which
 * the test moves with the returned setter.
 */
function equipLive(video: HTMLVideoElement, duration: number, front: number) {
  let currentTime = 0
  let seekableEnd = front
  let mediaDuration = duration
  let paused = true
  Object.defineProperties(video, {
    currentTime: {
      configurable: true,
      get: () => currentTime,
      set: (value: number) => {
        currentTime = value
      },
    },
    duration: { configurable: true, get: () => mediaDuration },
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
    seekable: {
      configurable: true,
      get: (): TimeRanges => ({
        length: 1,
        start: () => 0,
        end: () => seekableEnd,
      }),
    },
  })
  return {
    setFront: (value: number) => {
      seekableEnd = value
    },
    setDuration: (value: number) => {
      mediaDuration = value
    },
  }
}

async function liveVideo(duration = 300, front = 300) {
  await waitFor(() => {
    expect(instances).toHaveLength(1)
  })
  const video = document.querySelector('video')
  if (video === null) throw new Error('no video element rendered')
  const control = equipLive(video, duration, front)
  return { video, ...control }
}

async function loadMetadata(video: HTMLVideoElement): Promise<void> {
  await act(async () => {
    fireEvent.loadedMetadata(video)
    await Promise.resolve()
    await Promise.resolve()
  })
}

/** Lets the front check (every second) and any poll run. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

function slider(): HTMLElement {
  const track = screen.getByRole('slider', { name: 'Seek' })
  // One pixel per second of the expected 1440.
  vi.spyOn(track, 'getBoundingClientRect').mockReturnValue({ left: 0, width: EXPECTED } as DOMRect)
  return track
}

function widthOf(testId: string): number {
  return Number.parseFloat(screen.getByTestId(testId).style.width)
}

let canPlayTypeSpy: MockInstance<HTMLMediaElement['canPlayType']> | null = null

beforeEach(() => {
  resetHls()
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
  canPlayTypeSpy?.mockRestore()
  canPlayTypeSpy = null
  vi.unstubAllGlobals()
})

describe('the live player (FR-P8)', () => {
  it('measures the episode by the server’s expected duration and draws the front', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo() } })
    const { video, setFront } = await liveVideo(300, 300)
    await loadMetadata(video)

    // The bar is 1440 s long, not the element's 300.
    expect(slider()).toHaveAttribute('aria-valuemax', String(EXPECTED))
    expect(widthOf('live-front')).toBeCloseTo((300 / EXPECTED) * 100)

    setFront(360)
    await advance(1000)
    expect(widthOf('live-front')).toBeCloseTo((360 / EXPECTED) * 100)
  })

  it('starts hls.js at 0 with the live options, never at the front', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo() } })
    await liveVideo()

    const config = instances[0]?.config
    expect(config?.startPosition).toBe(0)
    expect(config?.maxLiveSyncPlaybackRate).toBe(1)
    expect(config?.liveMaxLatencyDuration).toBeUndefined()
    expect(config?.liveSyncDuration).toBeGreaterThan(EXPECTED)
  })

  it('turns a seek past the front into a pending seek, then jumps at target + 6', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo() } })
    const { video, setFront } = await liveVideo(300, 300)
    await loadMetadata(video)

    fireEvent.pointerDown(slider(), { clientX: 750 })

    expect(video.currentTime).toBe(299)
    expect(screen.getByTestId('live-status')).toHaveTextContent(
      'Downloading — 43 % · 12 seeders · will jump to 12:30',
    )

    setFront(755)
    await advance(1000)
    expect(video.currentTime).toBe(299)

    setFront(756)
    await advance(1000)
    expect(video.currentTime).toBe(750)
    expect(screen.queryByTestId('live-status')).not.toBeInTheDocument()
  })

  it('seeks below the front at once, with nothing pending', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo() } })
    const { video } = await liveVideo(300, 300)
    await loadMetadata(video)

    fireEvent.pointerDown(slider(), { clientX: 120 })

    expect(video.currentTime).toBe(120)
    expect(screen.queryByTestId('live-status')).not.toBeInTheDocument()
  })

  it('holds the arrow keys and End to the front (FR-S6)', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo() } })
    const { video } = await liveVideo(300, 300)
    await loadMetadata(video)
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

    video.currentTime = 100
    await user.keyboard('{ArrowRight}')
    expect(video.currentTime).toBe(105)

    video.currentTime = 297
    await user.keyboard('{ArrowRight}')
    expect(video.currentTime).toBe(299)

    video.currentTime = 120
    await user.keyboard('{End}')
    expect(video.currentTime).toBe(299)
    // A clamp is not a pending seek: nothing says "will jump".
    expect(screen.queryByText(/will jump/)).not.toBeInTheDocument()
  })

  it('waits with a resume beyond the front, then resumes (FR-S2)', async () => {
    renderPlayer({ [PLAY]: { body: { ...liveInfo(), resume_position: 750 } } })
    const { video, setFront } = await liveVideo(300, 300)
    // hls.js loads from what it can reach, not from past the front.
    expect(instances[0]?.config.startPosition).toBe(299)

    await loadMetadata(video)
    expect(video.currentTime).toBe(299)
    expect(screen.getByTestId('live-status')).toHaveTextContent('will jump to 12:30')
    expect(screen.queryByText('Resumed from 12:30')).not.toBeInTheDocument()

    setFront(800)
    await advance(1000)
    expect(video.currentTime).toBe(750)
    expect(screen.getByText('Resumed from 12:30')).toBeInTheDocument()
  })

  it('polls /play every 5 s and stops once the download has ended', async () => {
    const routes: MockRoutes = { [PLAY]: { body: liveInfo() } }
    const { fetchMock } = renderPlayer(routes)
    const { video } = await liveVideo()
    await loadMetadata(video)
    expect(polls(fetchMock)).toBe(1)

    routes[PLAY] = { body: liveInfo({ front_seconds: 400, download_progress: 0.6 }) }
    await advance(LIVE_POLL_MS)
    expect(polls(fetchMock)).toBe(2)

    routes[PLAY] = {
      body: liveInfo({ front_seconds: EXPECTED, download_progress: 1, ended: true }),
    }
    await advance(LIVE_POLL_MS)
    expect(polls(fetchMock)).toBe(3)
    // The front bar goes with the download.
    await waitFor(() => {
      expect(screen.queryByTestId('live-front')).not.toBeInTheDocument()
    })

    await advance(LIVE_POLL_MS * 3)
    expect(polls(fetchMock)).toBe(3)
    expect(instances).toHaveLength(1)
  })

  it('stops polling on adoption and keeps playing the same URL with no reload', async () => {
    const routes: MockRoutes = { [PLAY]: { body: liveInfo() } }
    const { fetchMock } = renderPlayer(routes)
    const { video } = await liveVideo()
    await loadMetadata(video)

    // Adopted: ready, the same playlist URL, no `live`.
    routes[PLAY] = { body: { ...liveInfo(), live: undefined } }
    await advance(LIVE_POLL_MS)
    expect(polls(fetchMock)).toBe(2)
    await waitFor(() => {
      expect(screen.queryByTestId('live-front')).not.toBeInTheDocument()
    })

    await advance(LIVE_POLL_MS * 3)
    expect(polls(fetchMock)).toBe(2)
    expect(instances).toHaveLength(1)
    expect(instances[0]?.loadSource).toHaveBeenCalledTimes(1)
    expect(instances[0]?.destroy).not.toHaveBeenCalled()
  })

  it('keeps the video up through a failed poll', async () => {
    const routes: MockRoutes = { [PLAY]: { body: liveInfo() } }
    const { fetchMock } = renderPlayer(routes)
    const { video } = await liveVideo()
    await loadMetadata(video)

    routes[PLAY] = { status: 503, body: { detail: 'down' } }
    await advance(LIVE_POLL_MS)
    expect(document.querySelector('video')).toBe(video)
    expect(screen.queryByText('Could not load this episode')).not.toBeInTheDocument()

    routes[PLAY] = { body: liveInfo({ front_seconds: 500 }) }
    await advance(LIVE_POLL_MS)
    expect(polls(fetchMock)).toBe(3)
  })

  it('feeds the reporter 1440 while the element says 300, and no receipt before 90 % of it', async () => {
    const { fetchMock } = renderPlayer({ [PLAY]: { body: liveInfo() } })
    const { video } = await liveVideo(300, 300)
    await loadMetadata(video)

    // 97 % of what the element knows; 20 % of the episode.
    video.currentTime = 290
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)
    await advance(100)

    const sent = fetchMock.mock.calls
      .filter(([, init]) => init?.method === 'POST')
      .map(([, init]) => jsonBodyOf(init))
    expect(sent.length).toBeGreaterThan(0)
    for (const body of sent) expect(body.duration_s).toBe(EXPECTED)
    expect(screen.queryByText('Marked as watched')).not.toBeInTheDocument()

    // Past 0.9 × 1440 = 1296: now the server's completion lands.
    video.currentTime = 1300
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)
    expect(await screen.findByText('Marked as watched')).toBeInTheDocument()
  })

  it('says few seeders below three, and not otherwise', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo({ seeders: 2 }) } })
    const { video } = await liveVideo()
    await loadMetadata(video)

    expect(screen.getByText('Few seeders — the download may be slow.')).toBeInTheDocument()
  })

  it('does not say few seeders with twelve', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo({ seeders: 12 }) } })
    const { video } = await liveVideo()
    await loadMetadata(video)

    expect(screen.queryByText(/Few seeders/)).not.toBeInTheDocument()
  })

  it('says it is downloading when playback has caught up with the front', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo() } })
    const { video } = await liveVideo(300, 300)
    await loadMetadata(video)
    expect(screen.queryByTestId('live-status')).not.toBeInTheDocument()

    video.currentTime = 298.5
    fireEvent.timeUpdate(video)

    expect(screen.getByTestId('live-status')).toHaveTextContent('Downloading — 43 % · 12 seeders')
  })
})

describe('the live player on Safari (native HLS)', () => {
  it('uses the element’s seekable range for a pending seek', async () => {
    canPlayTypeSpy = vi
      .spyOn(window.HTMLMediaElement.prototype, 'canPlayType')
      .mockReturnValue('probably')
    vi.stubGlobal('MediaSource', undefined)
    renderPlayer({ [PLAY]: { body: liveInfo({ front_seconds: 280 }) } })

    await waitFor(() => {
      expect(document.querySelector('video')).toHaveAttribute('src', PLAY_INFO.playlist_url)
    })
    const video = document.querySelector('video')
    if (video === null) throw new Error('no video')
    // The element's front (300) is ahead of the last poll's (280).
    const { setFront } = equipLive(video, Number.POSITIVE_INFINITY, 300)
    video.currentTime = 300
    await loadMetadata(video)
    expect(instances).toHaveLength(0)
    // Put at the start rather than the edge Safari chose.
    expect(video.currentTime).toBe(0)

    fireEvent.pointerDown(slider(), { clientX: 750 })
    expect(video.currentTime).toBe(299)
    expect(screen.getByTestId('live-status')).toHaveTextContent('will jump to 12:30')

    setFront(756)
    await advance(1000)
    expect(video.currentTime).toBe(750)
  })
})

describe('the front a seek may use', () => {
  it('is the element’s seekable end, not the server’s number (Safari holds back)', async () => {
    // The server has 300 s; Safari's range stops 60 s short of its playlist.
    renderPlayer({ [PLAY]: { body: liveInfo({ front_seconds: 300 }) } })
    const { video } = await liveVideo(Number.POSITIVE_INFINITY, 240)
    await loadMetadata(video)
    await advance(1000)

    // The bar never moves back to Safari's shorter range: it shows the larger.
    expect(widthOf('live-front')).toBeCloseTo((300 / EXPECTED) * 100)

    fireEvent.pointerDown(slider(), { clientX: 270 })
    expect(video.currentTime).toBe(239)
    expect(screen.getByTestId('live-status')).toHaveTextContent('will jump to 4:30')
  })

  it('seeks backwards at once from past Safari’s range', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo({ front_seconds: 40 }) } })
    const { video } = await liveVideo(Number.POSITIVE_INFINITY, 16)
    await loadMetadata(video)
    video.currentTime = 30

    fireEvent.pointerDown(slider(), { clientX: 20 })

    expect(video.currentTime).toBe(20)
    expect(screen.queryByText(/will jump/)).not.toBeInTheDocument()
  })

  it('seeks backwards at once while the element reports no range at all', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo({ front_seconds: 40 }) } })
    const { video } = await liveVideo(Number.NaN, 0)
    await loadMetadata(video)
    video.currentTime = 30

    fireEvent.pointerDown(slider(), { clientX: 10 })

    expect(video.currentTime).toBe(10)
  })

  it('moves → forward from past Safari’s range, up to the server’s front', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo({ front_seconds: 40 }) } })
    const { video } = await liveVideo(Number.POSITIVE_INFINITY, 16)
    await loadMetadata(video)
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

    video.currentTime = 30
    await user.keyboard('{ArrowRight}')
    expect(video.currentTime).toBe(35)

    await user.keyboard('{ArrowRight}')
    expect(video.currentTime).toBe(39)
  })

  it('reaches nothing while the element has no range, and does not pull the playhead back', async () => {
    renderPlayer({ [PLAY]: { body: liveInfo({ front_seconds: 300 }) } })
    const { video, setFront } = await liveVideo(Number.NaN, 0)
    await loadMetadata(video)
    video.currentTime = 5

    fireEvent.pointerDown(slider(), { clientX: 100 })
    expect(video.currentTime).toBe(5)
    expect(screen.getByTestId('live-status')).toHaveTextContent('will jump to 1:40')
    // The bar draws the server's number while the element has none.
    expect(widthOf('live-front')).toBeCloseTo((300 / EXPECTED) * 100)

    setFront(120)
    await advance(1000)
    expect(video.currentTime).toBe(100)
  })
})

/** Every progress body the page posted, in order. */
function posted(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown>[] {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === 'POST')
    .map(([, init]) => jsonBodyOf(init as RequestInit))
}

describe('the live player after the download (FR-S4, FR-P8)', () => {
  it('keeps measuring by the expected duration after adoption until the element catches up', async () => {
    const routes: MockRoutes = { [PLAY]: { body: liveInfo({ front_seconds: 1200 }) } }
    const { fetchMock } = renderPlayer(routes)
    const { video, setDuration } = await liveVideo(1200, 1200)
    await loadMetadata(video)

    // Adopted between two polls; hls.js still holds the 1200 s playlist.
    routes[PLAY] = { body: { ...liveInfo(), live: undefined } }
    await advance(LIVE_POLL_MS + 100)
    video.currentTime = 1100
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)
    await advance(500)

    expect(posted(fetchMock).at(-1)?.duration_s).toBe(EXPECTED)
    expect(screen.queryByText('Marked as watched')).not.toBeInTheDocument()

    // The element has the whole episode now: its own duration counts again.
    setDuration(1441)
    video.currentTime = 1300
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)
    expect(await screen.findByText('Marked as watched')).toBeInTheDocument()
    expect(posted(fetchMock).at(-1)?.duration_s).toBe(1441)
  })

  it('reports the resume target, not the parked playhead, while the resume waits (FR-S2)', async () => {
    const { fetchMock } = renderPlayer({
      [PLAY]: { body: { ...liveInfo(), resume_position: 750 } },
    })
    const { video } = await liveVideo(300, 300)
    await loadMetadata(video)
    expect(video.currentTime).toBe(299)

    fireEvent.timeUpdate(video)
    fireEvent.pause(video)
    await advance(500)

    const bodies = posted(fetchMock)
    expect(bodies.length).toBeGreaterThan(0)
    for (const body of bodies) expect(body.position_s).toBe(750)
  })

  it('on adoption, waits for the element’s range before jumping', async () => {
    const routes: MockRoutes = { [PLAY]: { body: liveInfo() } }
    renderPlayer(routes)
    const { video, setFront } = await liveVideo(300, 300)
    await loadMetadata(video)
    fireEvent.pointerDown(slider(), { clientX: 750 })
    expect(video.currentTime).toBe(299)

    routes[PLAY] = { body: { ...liveInfo(), live: undefined } }
    await advance(LIVE_POLL_MS + 100)
    await advance(2000)
    // hls.js has not reloaded the playlist: 750 is not reachable yet.
    expect(video.currentTime).toBe(299)

    setFront(EXPECTED)
    await advance(1000)
    expect(video.currentTime).toBe(750)
  })

  it('after ENDLIST, resolves a target past the real end to the end', async () => {
    const routes: MockRoutes = { [PLAY]: { body: liveInfo() } }
    renderPlayer(routes)
    const { video, setFront } = await liveVideo(300, 300)
    await loadMetadata(video)
    // The expected 1440 is 20 s longer than the file turns out to be.
    fireEvent.pointerDown(slider(), { clientX: 1435 })
    expect(screen.getByTestId('live-status')).toHaveTextContent('will jump to 23:55')

    routes[PLAY] = { body: liveInfo({ front_seconds: 1420, download_progress: 1, ended: true }) }
    setFront(1420)
    await advance(LIVE_POLL_MS + 100)
    await advance(1000)

    expect(video.currentTime).toBe(1419)
  })
})

describe('a device copy of an episode that is still downloading', () => {
  function pretendOpfs() {
    Object.defineProperty(navigator, 'storage', {
      configurable: true,
      value: { getDirectory: () => Promise.reject(new Error('not in tests')) },
    })
    vi.stubGlobal('Worker', class {})
  }

  afterEach(() => {
    Reflect.deleteProperty(navigator, 'storage')
    setDownloads(null)
  })

  it('shows no live UI and does not poll', async () => {
    pretendOpfs()
    const harness = managerHarness({ initial: [recordEntry(downloadedRecord(TEST_USER.id))] })
    const record = downloadedRecord(TEST_USER.id)
    harness.files.set(record.name, record.bytes)
    setDownloads(harness.manager)

    const { fetchMock } = renderPlayer(
      { [PLAY]: { body: liveInfo({ front_seconds: 0, seeders: 2 }) } },
      true,
    )
    const video = await waitFor(
      () => {
        const found = document.querySelector('video')
        if (found === null || found.getAttribute('src') === null) throw new Error('no source yet')
        return found
      },
      { timeout: 4000 },
    )
    expect(video.getAttribute('src')).toMatch(/^blob:/)
    await loadMetadata(video)
    await advance(LIVE_POLL_MS * 3)

    expect(instances).toHaveLength(0)
    expect(screen.queryByTestId('live-front')).not.toBeInTheDocument()
    expect(screen.queryByTestId('live-status')).not.toBeInTheDocument()
    expect(screen.queryByText(/Few seeders/)).not.toBeInTheDocument()
    expect(polls(fetchMock)).toBeLessThanOrEqual(1)
  })
})
