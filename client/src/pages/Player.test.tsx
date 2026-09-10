import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import type { PlayInfo } from '@/lib/playback'
import { createQueryClient } from '@/lib/queryClient'
import { Player } from '@/pages/Player'
import {
  FRIEREN,
  PLAY_INFO,
  PLAY_INFO_EPISODE_2,
  PLAY_INFO_LAST,
  PLAY_INFO_NEXT_READY,
} from '@/test/animeFixtures'
import { jsonBodyOf, mockApi, requestsMade, TEST_USER, type MockRoutes } from '@/test/apiMock'
import FakeHls, { Events, instances, resetHls } from '@/test/hlsMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

const EPISODE_ID = 9001
const PLAY_PATH = `GET /api/episodes/${String(EPISODE_ID)}/play`
const PROGRESS_PATH = 'POST /api/progress'
const PROGRESS_RESULT = { completed: false, newly_completed: false, list_progress: null }
const PROGRESS_UNSAVED = 'Progress isn’t being saved — check your connection.'

function renderPlayer(routes: MockRoutes = { [PLAY_PATH]: { body: PLAY_INFO } }) {
  const fetchMock = mockApi({
    'GET /api/auth/me': { body: TEST_USER },
    'POST /api/progress': { body: PROGRESS_RESULT },
    ...routes,
  })
  const router = createMemoryRouter(
    [
      { path: '/watch/:episodeId', element: <Player /> },
      { path: '/anime/:id', element: <p>show page</p> },
      { path: '/', element: <p>home</p> },
    ],
    { initialEntries: [`/watch/${String(EPISODE_ID)}`] },
  )
  const view = render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { fetchMock, view }
}

/**
 * jsdom's media element is inert: `play`, `pause` and setting `currentTime`
 * are all unimplemented, and `duration` is NaN. These own properties shadow
 * the prototype so the shortcuts and the resume have something real to move.
 */
function equipVideo(video: HTMLVideoElement, duration = 1436.8): void {
  let paused = true
  let currentTime = 0
  let muted = false

  Object.defineProperties(video, {
    paused: { configurable: true, get: () => paused },
    currentTime: {
      configurable: true,
      get: () => currentTime,
      set: (value: number) => {
        currentTime = value
      },
    },
    duration: { configurable: true, get: () => duration },
    muted: {
      configurable: true,
      get: () => muted,
      set: (value: boolean) => {
        muted = value
      },
    },
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

/** Waits for the lazy `import('hls.js')` to land, then returns the video. */
async function readyVideo(equip = true): Promise<HTMLVideoElement> {
  await waitFor(() => {
    expect(instances).toHaveLength(1)
  })
  const video = document.querySelector('video')
  if (video === null) throw new Error('no video element rendered')
  if (equip) equipVideo(video)
  return video
}

/**
 * The two things the native-vs-hls.js decision reads, neither of which jsdom
 * has an honest answer for: it never defines `MediaSource`, and its
 * `canPlayType` always says `''`. Restored in `afterEach`.
 */
let canPlayTypeSpy: MockInstance<HTMLMediaElement['canPlayType']> | null = null

function stubBrowserSupport(answer: CanPlayTypeResult, mediaSource: boolean): void {
  canPlayTypeSpy = vi
    .spyOn(window.HTMLMediaElement.prototype, 'canPlayType')
    .mockReturnValue(answer)
  vi.stubGlobal('MediaSource', mediaSource ? class FakeMediaSource {} : undefined)
}

beforeEach(() => {
  resetHls()
})

afterEach(() => {
  canPlayTypeSpy?.mockRestore()
  canPlayTypeSpy = null
  vi.unstubAllGlobals()
})

describe('Player', () => {
  it('mounts hls.js on the playlist the server named, with credentials (FR-S1)', async () => {
    renderPlayer()

    await readyVideo(false)
    const hls = instances[0]
    expect(hls?.loadSource).toHaveBeenCalledExactlyOnceWith(PLAY_INFO.playlist_url)
    expect(hls?.attachMedia).toHaveBeenCalledTimes(1)

    // Playlists and segments sit behind the session cookie (spec §5.4).
    const xhr = { withCredentials: false } as XMLHttpRequest
    hls?.config.xhrSetup?.(xhr, PLAY_INFO.playlist_url)
    expect(xhr.withCredentials).toBe(true)
  })

  /**
   * The native path is for the browser that has no Media Source Extensions at
   * all (iOS Safari): it gets the playlist as a `src` and hls.js is never even
   * fetched, which is the whole point of importing it lazily.
   */
  it('hands a browser with no MediaSource the playlist itself, without hls.js', async () => {
    stubBrowserSupport('probably', false)
    renderPlayer()

    const video = await screen.findByLabelText(`${FRIEREN.title.preferred} — Episode 1`)
    expect(video).toHaveAttribute('src', PLAY_INFO.playlist_url)
    expect(instances).toHaveLength(0)
  })

  /**
   * Chrome answers `'maybe'` for the playlist mime and then cannot play one,
   * so `canPlayType` must not be allowed to win where MSE exists — believing
   * it left the player on a spinner forever.
   */
  it('uses hls.js where MediaSource exists, whatever canPlayType claims', async () => {
    stubBrowserSupport('maybe', true)
    renderPlayer()

    await readyVideo(false)
    expect(instances[0]?.loadSource).toHaveBeenCalledExactlyOnceWith(PLAY_INFO.playlist_url)
    // Nothing was handed to the element directly; hls.js feeds it.
    expect(document.querySelector('video')).not.toHaveAttribute('src')
  })

  it('says so when hls.js cannot run here either', async () => {
    FakeHls.isSupported.mockReturnValue(false)
    renderPlayer()

    expect(await screen.findByText('This browser cannot play HLS.')).toBeInTheDocument()
    expect(instances).toHaveLength(0)
  })

  it('shows the show, the episode and the neighbours it can offer', async () => {
    renderPlayer()

    expect(
      await screen.findByRole('heading', { name: FRIEREN.title.preferred }),
    ).toBeInTheDocument()
    expect(screen.getByText('Episode 1')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to show' })).toHaveAttribute(
      'href',
      `/anime/${String(FRIEREN.id)}`,
    )
    // There is no previous episode, so no control at all for it.
    expect(screen.queryByText(/^Previous/)).not.toBeInTheDocument()
    // The next one exists but is still being prepared: shown, disabled, explained.
    const next = screen.getByRole('button', { name: 'Next · Episode 2' })
    expect(next).toBeDisabled()
    expect(next).toHaveAttribute('title', 'Episode 2 isn’t ready yet')
  })

  it('gives the video an accessible name naming show and episode', async () => {
    renderPlayer()

    const labelled = await screen.findByLabelText(`${FRIEREN.title.preferred} — Episode 1`)
    expect(labelled.tagName).toBe('VIDEO')
  })

  it('links a ready next episode straight to its own player page', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    expect(await screen.findByRole('link', { name: 'Next · Episode 2' })).toHaveAttribute(
      'href',
      '/watch/9002',
    )
  })

  it('resumes where the viewer left off and says that it did (FR-S2)', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)

    expect(video.currentTime).toBe(PLAY_INFO.resume_position)
    expect(await screen.findByText('Resumed from 12:34')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('Resumed from 12:34')).not.toBeInTheDocument()
  })

  it('does not resume when there is nothing to resume from', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_LAST } })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)

    expect(video.currentTime).toBe(0)
    expect(screen.queryByText(/^Resumed from/)).not.toBeInTheDocument()
  })

  it('leaves a position past 95 % of the episode alone (FR-S2)', async () => {
    const nearlyDone: PlayInfo = { ...PLAY_INFO, resume_position: 1420 }
    renderPlayer({ [PLAY_PATH]: { body: nearlyDone } })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)

    expect(video.currentTime).toBe(0)
  })

  it('answers the keyboard from the page, not only from the video (FR-S6)', async () => {
    renderPlayer()

    const video = await readyVideo()
    expect(video.paused).toBe(true)

    fireEvent.keyDown(window, { key: ' ' })
    expect(video.paused).toBe(false)
    fireEvent.keyDown(window, { key: ' ' })
    expect(video.paused).toBe(true)

    video.currentTime = 100
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(video.currentTime).toBe(105)
    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    expect(video.currentTime).toBe(100)

    // Never below zero, however many times the viewer holds the key.
    video.currentTime = 2
    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    expect(video.currentTime).toBe(0)

    expect(video.muted).toBe(false)
    fireEvent.keyDown(window, { key: 'm' })
    expect(video.muted).toBe(true)
  })

  it('reports the position on pause (FR-S3)', async () => {
    const { fetchMock } = renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    video.currentTime = 900
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/progress')
    })
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(jsonBodyOf(post?.[1])).toEqual({
      episode_id: EPISODE_ID,
      position_s: 900,
      duration_s: 1436.8,
    })
  })

  it('offers the next episode when the current one ends (FR-S5)', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    const video = await readyVideo()
    fireEvent.ended(video)

    expect(await screen.findByText('Next: Episode 2')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Play' })).toHaveAttribute('href', '/watch/9002')
    // The header's back link and the overlay's own both point at the show.
    expect(screen.getAllByRole('link', { name: 'Back to show' })).toHaveLength(2)
  })

  it('names the next episode without offering it when it is not ready', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.ended(video)

    expect(await screen.findByText('Episode 2 isn’t ready yet')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Play' })).not.toBeInTheDocument()
  })

  it('says so when there is no next episode', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_LAST } })

    const video = await readyVideo()
    fireEvent.ended(video)

    expect(await screen.findByText('This was the last episode')).toBeInTheDocument()
  })

  it('shows the overlay as soon as the server calls the episode complete (FR-S4)', async () => {
    renderPlayer({
      [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY },
      'POST /api/progress': {
        body: { completed: true, newly_completed: true, list_progress: 1 },
      },
    })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    video.currentTime = 1350
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)

    expect(await screen.findByText('Next: Episode 2')).toBeInTheDocument()
  })

  it('surfaces a fatal hls error with a way to try again', async () => {
    renderPlayer()

    await readyVideo(false)
    const hls = instances[0]
    fireEvent.pause(document.querySelector('video') as HTMLVideoElement)
    hls?.emit(Events.ERROR, { fatal: true, type: 'networkError' })

    const alerts = await screen.findAllByRole('alert')
    expect(alerts.some((node) => node.textContent?.includes('Playback stopped'))).toBe(true)

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => {
      expect(instances).toHaveLength(2)
    })
    expect(hls?.destroy).toHaveBeenCalled()
  })

  it('puts a recovered stream back where the viewer was, not at zero', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    expect(video.currentTime).toBe(PLAY_INFO.resume_position)

    video.currentTime = 900
    fireEvent.timeUpdate(video)

    instances[0]?.emit(Events.ERROR, { fatal: true, type: 'networkError' })
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => {
      expect(instances).toHaveLength(2)
    })

    // The rebuilt stream comes back at the start; the page must not accept it.
    video.currentTime = 0
    fireEvent.loadedMetadata(video)
    expect(video.currentTime).toBe(900)
  })

  /**
   * The play call carries the resume position, so a cached one is wrong the
   * moment the viewer comes back from another episode (they may have watched
   * more of this one, or the same one, since).
   */
  it('re-reads the play info when the viewer returns to an episode', async () => {
    const { fetchMock } = renderPlayer({
      [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY },
      'GET /api/episodes/9002/play': { body: PLAY_INFO_EPISODE_2 },
    })

    await userEvent.click(await screen.findByRole('link', { name: 'Next · Episode 2' }))
    await userEvent.click(await screen.findByRole('link', { name: 'Previous · Episode 1' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === PLAY_PATH)).toHaveLength(2)
    })
  })

  it('tells the viewer the episode is not ready when the server says 404', async () => {
    renderPlayer({ [PLAY_PATH]: { status: 404, body: { detail: 'episode not ready' } } })

    expect(await screen.findByRole('heading', { name: 'Episode not ready' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to home' })).toHaveAttribute('href', '/')
    expect(document.querySelector('video')).toBeNull()
  })

  it('offers a retry when the play info could not be loaded at all', async () => {
    const { fetchMock } = renderPlayer({ [PLAY_PATH]: { status: 500, body: { detail: 'boom' } } })

    expect(
      await screen.findByRole('heading', { name: 'Could not load this episode' }),
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === PLAY_PATH)).toHaveLength(2)
    })
  })

  it('does not call the API for a non-numeric episode id', () => {
    const fetchMock = mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    const router = createMemoryRouter([{ path: '/watch/:episodeId', element: <Player /> }], {
      initialEntries: ['/watch/nope'],
    })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(screen.getByRole('heading', { name: 'Episode not ready' })).toBeInTheDocument()
    expect(requestsMade(fetchMock)).not.toContain('GET /api/episodes/NaN/play')
  })

  it('destroys the hls instance when the page goes away', async () => {
    const { view } = renderPlayer()

    await readyVideo(false)
    view.unmount()

    expect(instances[0]?.destroy).toHaveBeenCalledTimes(1)
  })
})

/**
 * A progress write that does not land is invisible by design — nothing about
 * the video changes — so the only thing that tells a viewer their place is
 * being lost is this strip. One miss is not worth saying anything about; a run
 * of them is.
 */
describe('Player progress reporting failures', () => {
  const FAILING_PROGRESS = { status: 500, body: { detail: 'boom' } }

  /**
   * One forced report. `pause` bypasses the reporter's minimum gap, so each
   * call is exactly one write — and no `loadedmetadata` is fired, which keeps
   * the resume toast (and its own Dismiss) out of the way.
   */
  function reportAt(video: HTMLVideoElement, position: number): void {
    video.currentTime = position
    fireEvent.timeUpdate(video)
    fireEvent.pause(video)
  }

  async function progressWrites(
    fetchMock: ReturnType<typeof mockApi>,
    count: number,
  ): Promise<void> {
    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === PROGRESS_PATH)).toHaveLength(count)
    })
  }

  function banner(): HTMLElement {
    const strip = screen.getByText(PROGRESS_UNSAVED).closest('[role="status"]')
    if (strip === null) throw new Error('the warning is not in a status region')
    return strip as HTMLElement
  }

  /** A run of failed writes, one at a time: each is a separate mutation. */
  async function failWrites(
    video: HTMLVideoElement,
    fetchMock: ReturnType<typeof mockApi>,
    positions: number[],
  ): Promise<void> {
    let sent = requestsMade(fetchMock).filter((path) => path === PROGRESS_PATH).length
    for (const position of positions) {
      reportAt(video, position)
      await progressWrites(fetchMock, ++sent)
    }
  }

  function failingPlayer() {
    return renderPlayer({
      [PLAY_PATH]: { body: PLAY_INFO },
      'POST /api/progress': FAILING_PROGRESS,
    })
  }

  it('says nothing until three writes in a row have failed', async () => {
    const { fetchMock } = failingPlayer()
    const video = await readyVideo()

    await failWrites(video, fetchMock, [100])
    expect(screen.queryByText(PROGRESS_UNSAVED)).not.toBeInTheDocument()

    await failWrites(video, fetchMock, [200])
    expect(screen.queryByText(PROGRESS_UNSAVED)).not.toBeInTheDocument()

    await failWrites(video, fetchMock, [300])
    expect(await screen.findByText(PROGRESS_UNSAVED)).toBeInTheDocument()
    // Non-blocking: the video is still there and still playable.
    expect(document.querySelector('video')).toBeInTheDocument()
  })

  it('takes the warning back down as soon as a write lands', async () => {
    const { fetchMock } = failingPlayer()
    const video = await readyVideo()

    await failWrites(video, fetchMock, [100, 200, 300])
    await screen.findByText(PROGRESS_UNSAVED)

    // The connection comes back; the next report is the proof.
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [PLAY_PATH]: { body: PLAY_INFO },
      'POST /api/progress': { body: PROGRESS_RESULT },
    })
    reportAt(video, 400)

    await waitFor(() => {
      expect(screen.queryByText(PROGRESS_UNSAVED)).not.toBeInTheDocument()
    })
  })

  it('lets the viewer wave the warning away', async () => {
    const { fetchMock } = failingPlayer()
    const video = await readyVideo()

    await failWrites(video, fetchMock, [100, 200, 300])
    await screen.findByText(PROGRESS_UNSAVED)

    await userEvent.click(within(banner()).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(PROGRESS_UNSAVED)).not.toBeInTheDocument()

    // A fourth failure does not bring back something already waved away.
    await failWrites(video, fetchMock, [500])
    expect(screen.queryByText(PROGRESS_UNSAVED)).not.toBeInTheDocument()
  })
})
