import { QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
const MARKED_WATCHED = 'Marked as watched'

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
  let playbackRate = 1

  Object.defineProperties(video, {
    paused: { configurable: true, get: () => paused },
    playbackRate: {
      configurable: true,
      get: () => playbackRate,
      set: (value: number) => {
        playbackRate = value
      },
    },
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

/** Moves the playhead and lets the page see it, as a tick of playback would. */
function seekTo(video: HTMLVideoElement, position: number): void {
  video.currentTime = position
  fireEvent.timeUpdate(video)
}

/**
 * One forced progress write at `position`. `pause` bypasses the reporter's
 * minimum gap, so this is exactly one report — the one the server can answer
 * with `newly_completed`.
 */
function completeAt(video: HTMLVideoElement, position: number): void {
  seekTo(video, position)
  fireEvent.pause(video)
}

/** Every number in an SVG path, in the order it is written. */
function numbersIn(path: string): number[] {
  return (path.match(/-?\d*\.?\d+/g) ?? []).map(Number)
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

/**
 * jsdom implements no Fullscreen API whatever — no `requestFullscreen`, no
 * `exitFullscreen`, and a `fullscreenElement` that cannot be moved — so both
 * halves of the toggle and the state the page reads back are installed by hand
 * and taken off again in `afterEach`.
 */
let fullscreenStubbed = false

function stubFullscreen(): { request: ReturnType<typeof vi.fn>; exit: ReturnType<typeof vi.fn> } {
  const request = vi.fn(() => Promise.resolve())
  const exit = vi.fn(() => Promise.resolve())
  Object.defineProperty(Element.prototype, 'requestFullscreen', {
    configurable: true,
    writable: true,
    value: request,
  })
  Object.defineProperty(document, 'exitFullscreen', {
    configurable: true,
    writable: true,
    value: exit,
  })
  fullscreenStubbed = true
  return { request, exit }
}

/** What the browser would tell the page after it entered or left fullscreen. */
function enterFullscreen(): void {
  Object.defineProperty(document, 'fullscreenElement', {
    configurable: true,
    writable: true,
    value: document.body,
  })
  fireEvent(document, new Event('fullscreenchange'))
}

beforeEach(() => {
  resetHls()
})

afterEach(() => {
  canPlayTypeSpy?.mockRestore()
  canPlayTypeSpy = null
  if (fullscreenStubbed) {
    Reflect.deleteProperty(Element.prototype, 'requestFullscreen')
    Reflect.deleteProperty(document, 'exitFullscreen')
    Reflect.deleteProperty(document, 'fullscreenElement')
    fullscreenStubbed = false
  }
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
    // There is no previous episode: the arrow is still there, dimmed, rather
    // than the bar changing width between episodes.
    const previous = screen.getByRole('button', { name: 'Previous episode' })
    expect(previous).toBeDisabled()
    expect(previous).toHaveAttribute('title', 'No previous episode')
    // The next one exists but is still being prepared: shown, disabled, explained.
    const next = screen.getByRole('button', { name: 'Next episode 2' })
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

    expect(await screen.findByRole('link', { name: 'Next episode 2' })).toHaveAttribute(
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

  /**
   * The notice is a receipt for something that already happened, so it does not
   * wait to be acknowledged: five seconds and it goes on its own (owner,
   * 2026-09-12). Dismiss, tested above, is still the way to have it sooner.
   */
  it('takes the resume notice away after five seconds on its own (FR-S2)', async () => {
    renderPlayer()

    const video = await readyVideo()

    vi.useFakeTimers()
    try {
      act(() => {
        fireEvent.loadedMetadata(video)
      })
      expect(screen.getByText('Resumed from 12:34')).toBeInTheDocument()

      // Still up just short of the five seconds: it is a pause, not a blink.
      act(() => {
        vi.advanceTimersByTime(4500)
      })
      expect(screen.getByText('Resumed from 12:34')).toBeInTheDocument()

      act(() => {
        vi.advanceTimersByTime(600)
      })
      expect(screen.queryByText('Resumed from 12:34')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
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
    expect(screen.getByRole('link', { name: 'Next episode' })).toHaveAttribute(
      'href',
      '/watch/9002',
    )
    // The header keeps its own way back; the card carries a second one.
    expect(screen.getByRole('link', { name: 'Back to show' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to the show' })).toHaveAttribute(
      'href',
      `/anime/${String(FRIEREN.id)}`,
    )
    expect(screen.getByRole('button', { name: 'Keep watching' })).toBeInTheDocument()
  })

  it('names the next episode without offering it when it is not ready', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.ended(video)

    // The line says why there is nothing to press; no dead button is drawn
    // under a sentence that has already explained itself.
    expect(await screen.findByText('Episode 2 isn’t ready yet')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Next episode' })).not.toBeInTheDocument()
    expect(
      within(screen.getByRole('group', { name: 'End of episode' })).queryByRole('button', {
        name: 'Next episode',
      }),
    ).not.toBeInTheDocument()
  })

  it('says so when there is no next episode', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_LAST } })

    const video = await readyVideo()
    fireEvent.ended(video)

    expect(await screen.findByText('This was the last episode')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Next episode' })).not.toBeInTheDocument()
  })

  /**
   * The two moments, as the owner drew them on 2026-09-17. Crossing the
   * completion mark is bookkeeping and gets a receipt; the *end* of the
   * episode is the decision and gets the card. They used to be one moment, and
   * the card arrived ten minutes early.
   */
  it('shows a toast, and no card, when the server calls the episode complete (FR-S4)', async () => {
    renderPlayer({
      [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY },
      'POST /api/progress': {
        body: { completed: true, newly_completed: true, list_progress: 1 },
      },
    })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    // Past 90 % of 1436.8 s (1293.1) and still 2:16 short of the end, so this
    // is the completion mark and nothing else.
    completeAt(video, 1300)

    expect(await screen.findByText(MARKED_WATCHED)).toBeInTheDocument()
    expect(screen.queryByText('Next: Episode 2')).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()
  })

  it('raises the completion toast once, and takes it away after four seconds', async () => {
    renderPlayer({
      [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY },
      'POST /api/progress': {
        body: { completed: true, newly_completed: true, list_progress: 1 },
      },
    })

    const video = await readyVideo()

    // The clock has to be fake before the report lands, or the four seconds
    // are armed against the real one and cannot be wound on.
    vi.useFakeTimers()
    try {
      completeAt(video, 1300)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50)
      })
      // Once, not once per tick: the server calls a completion new exactly
      // once per (user, episode).
      expect(screen.getAllByText(MARKED_WATCHED)).toHaveLength(1)
      seekTo(video, 1305)
      expect(screen.getAllByText(MARKED_WATCHED)).toHaveLength(1)

      // Still up just short of the four seconds: a receipt, not a blink.
      act(() => {
        vi.advanceTimersByTime(3500)
      })
      expect(screen.getByText(MARKED_WATCHED)).toBeInTheDocument()

      act(() => {
        vi.advanceTimersByTime(600)
      })
      expect(screen.queryByText(MARKED_WATCHED)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  /**
   * A rewatch crosses the same mark and must say nothing: `newly_completed` is
   * the server's once-per-(user, episode) answer (architecture §5.4a), so the
   * second time through it is false and there is nothing to announce.
   */
  it('says nothing when a rewatch crosses the mark again (FR-S4)', async () => {
    const rewatch: PlayInfo = {
      ...PLAY_INFO_NEXT_READY,
      episode: { ...PLAY_INFO_NEXT_READY.episode, watched: true, watched_source: 'arc' },
    }
    const { fetchMock } = renderPlayer({
      [PLAY_PATH]: { body: rewatch },
      'POST /api/progress': {
        body: { completed: true, newly_completed: false, list_progress: null },
      },
    })

    const video = await readyVideo()
    completeAt(video, 1300)
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(PROGRESS_PATH)
    })

    expect(screen.queryByText(MARKED_WATCHED)).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()
  })

  it('brings the card up with a minute and a half left, not at the mark', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)

    // 2:16 left: past the completion mark, and still the middle of the episode.
    seekTo(video, 1300)
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()

    // 1:29 left.
    seekTo(video, 1348)
    expect(await screen.findByRole('group', { name: 'End of episode' })).toBeInTheDocument()
    expect(screen.getByText('Next: Episode 2')).toBeInTheDocument()
  })

  it('puts the card away on Keep watching and does not bring it back', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    seekTo(video, 1350)
    await screen.findByRole('group', { name: 'End of episode' })

    await userEvent.click(screen.getByRole('button', { name: 'Keep watching' }))
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()

    // The next tick is deeper into the window, and the media then ends: the
    // viewer has said no once and is not asked again this playback.
    seekTo(video, 1400)
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()
    fireEvent.ended(video)
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()
  })

  it('reads Escape as Keep watching', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    seekTo(video, 1350)
    await screen.findByRole('group', { name: 'End of episode' })

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => {
      expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()
    })

    seekTo(video, 1400)
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()
  })

  /** The card is a card, not a modal: the window shortcuts still answer. */
  it('leaves the playback shortcuts working while the card is up (FR-S6)', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    seekTo(video, 1350)
    await screen.findByRole('group', { name: 'End of episode' })

    // Nothing inside the card took focus away from the page.
    expect(document.activeElement).toBe(document.body)

    fireEvent.keyDown(window, { key: ' ' })
    expect(video.paused).toBe(false)
  })

  it('leaves for the show page from the card', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    const video = await readyVideo()
    fireEvent.ended(video)
    await screen.findByRole('group', { name: 'End of episode' })

    await userEvent.click(screen.getByRole('link', { name: 'Back to the show' }))
    expect(await screen.findByText('show page')).toBeInTheDocument()
  })

  it('goes to the next episode from the card', async () => {
    const { fetchMock } = renderPlayer({
      [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY },
      'GET /api/episodes/9002/play': { body: PLAY_INFO_EPISODE_2 },
    })

    const video = await readyVideo()
    fireEvent.ended(video)
    await screen.findByRole('group', { name: 'End of episode' })

    await userEvent.click(screen.getByRole('link', { name: 'Next episode' }))
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('GET /api/episodes/9002/play')
    })
    // A fresh page for the new episode: no card carried over from the old one.
    expect(await screen.findByText('Episode 2')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'End of episode' })).not.toBeInTheDocument()
  })

  /**
   * Both the receipt and the card live inside the element the page asks the
   * browser to make fullscreen; anywhere else they would be invisible for the
   * one mode in which an episode is most likely to be watched to the end.
   */
  it('renders the toast and the card inside the fullscreen element', async () => {
    const { request } = stubFullscreen()
    renderPlayer({
      [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY },
      'POST /api/progress': {
        body: { completed: true, newly_completed: true, list_progress: 1 },
      },
    })

    const video = await readyVideo()
    await userEvent.click(screen.getByRole('button', { name: 'Fullscreen' }))
    const root = request.mock.instances[0] as HTMLElement | undefined
    if (root === undefined) throw new Error('nothing was asked to go fullscreen')

    completeAt(video, 1300)
    expect(root.contains(await screen.findByText(MARKED_WATCHED))).toBe(true)

    fireEvent.ended(video)
    expect(root.contains(await screen.findByRole('group', { name: 'End of episode' }))).toBe(true)
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

    await userEvent.click(await screen.findByRole('link', { name: 'Next episode 2' }))
    await userEvent.click(await screen.findByRole('link', { name: 'Previous episode 1' }))

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
 * The controls the page draws itself (M15). The native ones are gone, so each
 * of these is now the only way a mouse reaches something the keyboard already
 * had — and each of them has to move the media element, not a copy of its
 * state held beside it.
 */
describe('Player controls', () => {
  /** The bar, or null once it has taken itself out of the accessibility tree. */
  function controlBar(): HTMLElement | null {
    return screen.queryByRole('group', { name: 'Player controls' })
  }

  it('draws no native controls: the page is the chrome', async () => {
    renderPlayer()

    const video = await readyVideo(false)
    expect(video).not.toHaveAttribute('controls')
    expect(controlBar()).toBeInTheDocument()
  })

  it('plays and pauses the media element', async () => {
    renderPlayer()

    const video = await readyVideo()
    const button = screen.getByRole('button', { name: 'Play or pause' })

    await userEvent.click(button)
    expect(video.paused).toBe(false)
    await userEvent.click(button)
    expect(video.paused).toBe(true)
  })

  it('moves the playhead by ten seconds, and never past the ends', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    video.currentTime = 100

    await userEvent.click(screen.getByRole('button', { name: 'Forward 10 seconds' }))
    expect(video.currentTime).toBe(110)
    await userEvent.click(screen.getByRole('button', { name: 'Back 10 seconds' }))
    expect(video.currentTime).toBe(100)

    video.currentTime = 4
    await userEvent.click(screen.getByRole('button', { name: 'Back 10 seconds' }))
    expect(video.currentTime).toBe(0)
  })

  /**
   * The owner's sign-off pass took both chips off the bar. The speed control
   * went entirely; mute kept its keyboard shortcut, which is the way anyone
   * actually reaches it, and lost the chip that was taking up a third of the
   * row to say something the viewer can already hear. The second pass took the
   * two `± 10s` text chips as well — the jump is now a round arrow with the
   * number inside it, so no word is left on the bar at all.
   */
  it('offers no speed, no volume and no text chips', async () => {
    renderPlayer()

    await readyVideo()
    expect(screen.queryByRole('button', { name: /speed/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /volume/i })).not.toBeInTheDocument()
    expect(screen.queryAllByText(/×/)).toHaveLength(0)
    expect(screen.queryByRole('button', { name: '+ 10s' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '− 10s' })).not.toBeInTheDocument()
    expect(screen.queryByText(/10s/)).not.toBeInTheDocument()
  })

  /**
   * The hint line is off the screen — it was a permanent row of text sitting on
   * top of burned-in subtitles — but nothing about it is lost: it hangs off the
   * play button, which is where a viewer looking for the transport already is.
   */
  it('keeps the shortcuts in the play button’s tooltip, not on the bar', async () => {
    renderPlayer()

    await readyVideo()

    expect(screen.queryByText('Space · ← → 5s · F · M')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Play or pause' })).toHaveAttribute(
      'title',
      'Play · Space · ← → 5s · F · M',
    )
  })

  it('still mutes from the keyboard with no chip to show for it', async () => {
    renderPlayer()

    const video = await readyVideo()
    expect(video.muted).toBe(false)

    fireEvent.keyDown(window, { key: 'm' })
    expect(video.muted).toBe(true)
    fireEvent.keyDown(window, { key: 'M' })
    expect(video.muted).toBe(false)
  })

  it('seeks to where the scrubber was clicked', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)

    const track = screen.getByRole('slider', { name: 'Seek' })
    // jsdom lays nothing out, so the track has to be told how wide it is.
    vi.spyOn(track, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 200,
    } as DOMRect)

    fireEvent.pointerDown(track, { clientX: 50 })
    expect(video.currentTime).toBeCloseTo(1436.8 * 0.25)
    expect(track).toHaveAttribute('aria-valuenow', String(Math.round(1436.8 * 0.25)))
  })

  it('answers Home and End as the ends of the episode (FR-S6)', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)

    fireEvent.keyDown(window, { key: 'End' })
    expect(video.currentTime).toBe(1436.8)
    fireEvent.keyDown(window, { key: 'Home' })
    expect(video.currentTime).toBe(0)
  })

  it('shows how far in and how far left', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    video.currentTime = 600
    fireEvent.timeUpdate(video)

    expect(screen.getByText('10:00')).toBeInTheDocument()
    expect(screen.getByText('-13:56')).toBeInTheDocument()
  })

  /**
   * A second and a half, and the mouse pointer goes with the bar: the owner's
   * complaint was that the chrome sits on top of the picture — and on the
   * subtitles burned into it — that it is there to serve. Nothing hides while
   * the video is paused.
   */
  it('gets out of the way after a second and a half, cursor and all', async () => {
    renderPlayer()

    const video = await readyVideo()
    const surface = video.parentElement
    expect(controlBar()).toBeInTheDocument()
    expect(surface).not.toHaveClass('cursor-none')

    vi.useFakeTimers()
    try {
      act(() => {
        void video.play()
      })
      // Still up at a second: the wait is shorter than it was, not none.
      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(controlBar()).toBeInTheDocument()

      act(() => {
        vi.advanceTimersByTime(700)
      })
      expect(controlBar()).toBeNull()
      expect(surface).toHaveClass('cursor-none')

      act(() => {
        fireEvent.pointerMove(window, { clientX: 10, clientY: 10 })
      })
      expect(controlBar()).toBeInTheDocument()
      expect(surface).not.toHaveClass('cursor-none')

      // A pause is a sign of a person, so the bar comes back — and then the
      // same timer takes it away again, because pausing is not a request to
      // keep it (owner, 2026-09-12).
      act(() => {
        video.pause()
      })
      expect(controlBar()).toBeInTheDocument()
      expect(surface).not.toHaveClass('cursor-none')

      act(() => {
        vi.advanceTimersByTime(1700)
      })
      expect(controlBar()).toBeNull()
      expect(surface).toHaveClass('cursor-none')
    } finally {
      vi.useRealTimers()
    }
  })

  /**
   * The timer is about the pointer, not about playback (owner, 2026-09-12).
   * Someone who pauses on a frame to read a sign in the background wants the
   * bar off the picture exactly as much as someone watching does, and one wave
   * of the mouse brings it back — so being wrong costs nothing.
   */
  it('hides while paused too, once the pointer has been still', async () => {
    renderPlayer()

    const video = await readyVideo()
    const surface = video.parentElement
    expect(controlBar()).toBeInTheDocument()

    vi.useFakeTimers()
    try {
      // The mouse moves, as it must have done for the bar to be up at all…
      act(() => {
        fireEvent.pointerMove(window, { clientX: 40, clientY: 40 })
      })
      expect(controlBar()).toBeInTheDocument()

      // …and then stops. This episode has never been played, and it still goes.
      act(() => {
        vi.advanceTimersByTime(1700)
      })
      expect(video.paused).toBe(true)
      expect(controlBar()).toBeNull()
      expect(surface).toHaveClass('cursor-none')

      act(() => {
        fireEvent.pointerMove(window, { clientX: 80, clientY: 80 })
      })
      expect(controlBar()).toBeInTheDocument()
      expect(surface).not.toHaveClass('cursor-none')
    } finally {
      vi.useRealTimers()
    }
  })

  /**
   * Two things outrank the timer because both are the page saying something
   * playback cannot show, and both put a control inside the bar's own fade
   * group that has to stay reachable while they are up.
   */
  it('holds the bar open while the end overlay is showing', async () => {
    renderPlayer({ [PLAY_PATH]: { body: PLAY_INFO_NEXT_READY } })

    const video = await readyVideo()
    expect(controlBar()).toBeInTheDocument()

    vi.useFakeTimers()
    try {
      act(() => {
        fireEvent.ended(video)
      })
      act(() => {
        vi.advanceTimersByTime(10_000)
      })
      expect(screen.getByText('Next: Episode 2')).toBeInTheDocument()
      expect(controlBar()).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  /** A drag still pins it: the track may not go out from under the finger. */
  it('holds the bar open while the scrubber is being dragged', async () => {
    renderPlayer()

    const video = await readyVideo()
    fireEvent.loadedMetadata(video)
    const track = screen.getByRole('slider', { name: 'Seek' })
    vi.spyOn(track, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 200,
    } as DOMRect)

    vi.useFakeTimers()
    try {
      act(() => {
        void video.play()
      })
      act(() => {
        fireEvent.pointerDown(track, { clientX: 50 })
      })
      act(() => {
        vi.advanceTimersByTime(10_000)
      })
      expect(controlBar()).toBeInTheDocument()

      // Let go, and the ordinary timer applies again.
      act(() => {
        fireEvent.pointerUp(track, { clientX: 50 })
      })
      act(() => {
        vi.advanceTimersByTime(1700)
      })
      expect(controlBar()).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  /** A pointer resting on the bar is not asking for anything (owner, M15). */
  it('hides even while the pointer is sitting still over the bar', async () => {
    renderPlayer()

    const video = await readyVideo()

    vi.useFakeTimers()
    try {
      act(() => {
        void video.play()
      })
      act(() => {
        fireEvent.pointerMove(window, { clientX: 10, clientY: 10 })
      })
      // No further moves: the pointer is parked, wherever it is parked.
      act(() => {
        vi.advanceTimersByTime(2200)
      })
      expect(controlBar()).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('marks the episode watched from the bar, and takes the mark off (FR-W3)', async () => {
    const unwatched: PlayInfo = { ...PLAY_INFO, episode: { ...PLAY_INFO.episode, watched: false } }
    const { fetchMock } = renderPlayer({
      [PLAY_PATH]: { body: unwatched },
      'POST /api/episodes/9001/watched': {
        body: { completed: true, newly_completed: false, list_progress: 1 },
      },
      'DELETE /api/episodes/9001/watched': {
        body: { completed: false, newly_completed: false, list_progress: 0 },
      },
    })

    const mark = await screen.findByRole('button', { name: 'Mark watched' })
    expect(mark).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(mark)

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/episodes/9001/watched')
    })
    const unmark = await screen.findByRole('button', { name: 'Unmark watched' })
    expect(unmark).toHaveAttribute('aria-pressed', 'true')

    await userEvent.click(unmark)
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('DELETE /api/episodes/9001/watched')
    })
    expect(await screen.findByRole('button', { name: 'Mark watched' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('offers no undo for an episode under the latest watched one (FR-W5)', async () => {
    // The tick is filled because the viewer has watched it, and this is not
    // the episode the un-mark would move: it lowers the list by one from the
    // top (FR-S4, revised 2026-09-13), so there is nothing here to press.
    const byList: PlayInfo = {
      ...PLAY_INFO,
      episode: { ...PLAY_INFO.episode, watched: true, watched_source: 'progress' },
    }
    const { fetchMock } = renderPlayer({
      [PLAY_PATH]: { body: byList },
      'DELETE /api/episodes/9001/watched': {
        body: { completed: false, newly_completed: false, list_progress: 1 },
      },
    })

    const pill = await screen.findByRole('button', { name: 'Watched' })
    expect(pill).toHaveAttribute('aria-pressed', 'true')
    expect(pill).toHaveAttribute('aria-disabled', 'true')
    expect(pill).toHaveAttribute('title', 'Unwatch from the latest watched episode down')
    expect(screen.queryByRole('button', { name: 'Unmark watched' })).not.toBeInTheDocument()

    await userEvent.click(pill)

    expect(requestsMade(fetchMock)).not.toContain('DELETE /api/episodes/9001/watched')
    // The glyph stays filled: the viewer has watched it either way.
    expect(pill.querySelector('circle')).toHaveClass('fill-current')
  })

  /**
   * The ⟲10 / ⟳10 pair had its arc on the wrong side (owner, 2026-09-17): the
   * back control has to read as a rewind — the ring open on the left, the head
   * turning counter-clockwise into that opening — and the forward one as the
   * same drawing mirrored, rather than as a second path that can drift.
   */
  it('draws the back skip as a rewind and the forward one as its mirror', async () => {
    renderPlayer()

    await readyVideo(false)
    const back = screen.getByRole('button', { name: 'Back 10 seconds' })
    const forward = screen.getByRole('button', { name: 'Forward 10 seconds' })

    const paths = (button: HTMLElement): string[] =>
      Array.from(button.querySelectorAll('svg g path')).map((path) => path.getAttribute('d') ?? '')

    // One drawing: the forward glyph is the rewind flipped about the vertical.
    expect(paths(back)).toEqual(paths(forward))
    expect(back.querySelector('svg g')?.getAttribute('transform')).toBeNull()
    expect(forward.querySelector('svg g')?.getAttribute('transform')).toBe(
      'translate(24 0) scale(-1 1)',
    )

    // `M<x> <y> A<rx> <ry> 0 1 1 <x2> <y2>`: both ends of the ring are left of
    // centre, so the quarter it is missing — the opening — is on the left.
    const ring = numbersIn(paths(back)[0] ?? '')
    const [ringX, ringY] = [ring[0] ?? NaN, ring[1] ?? NaN]
    expect(ringX).toBeLessThan(12)
    expect(ring[7]).toBeLessThan(12)

    // `M<x> <y> H<x2> V<y2>`: the head's corner is the ring's upper end, its
    // arms trailing right and up — an arrow pointing down into the opening,
    // which is anticlockwise, which is backwards.
    const head = numbersIn(paths(back)[1] ?? '')
    const [armX, armY, cornerX, topY] = [
      head[0] ?? NaN,
      head[1] ?? NaN,
      head[2] ?? NaN,
      head[3] ?? NaN,
    ]
    expect(cornerX).toBe(ringX)
    expect(armY).toBe(ringY)
    expect(armX).toBeGreaterThan(cornerX)
    expect(topY).toBeLessThan(armY)

    // The number reads through the middle of both, upright: it sits outside
    // the mirrored group, so "10" never comes out backwards.
    expect(back.querySelector('svg text')?.textContent).toBe('10')
    expect(forward.querySelector('svg text')?.textContent).toBe('10')
    expect(forward.querySelector('svg text')?.closest('g')).toBeNull()
  })

  /**
   * The words came off the bar; none of them came off the page. Every glyph
   * still says what it is to a screen reader and to a hover.
   */
  it('says in words what each glyph means', async () => {
    renderPlayer()

    await readyVideo()

    expect(screen.getByRole('button', { name: 'Play or pause' })).toBeInTheDocument()
    const fullscreen = screen.getByRole('button', { name: 'Fullscreen' })
    expect(fullscreen).toHaveAttribute('title', 'Fullscreen')
    expect(fullscreen).toHaveAttribute('aria-pressed', 'false')

    // This fixture's episode is already watched, so the mark is pressed.
    const watched = screen.getByRole('button', { name: 'Unmark watched' })
    expect(watched).toHaveAttribute('aria-pressed', 'true')
    expect(watched).toHaveAttribute('title', 'Unmark watched')

    // The two neighbours, and nothing else, still live in the Episodes nav.
    const nav = screen.getByRole('navigation', { name: 'Episodes' })
    expect(within(nav).getAllByRole('button')).toHaveLength(2)
    expect(within(nav).getByRole('button', { name: 'Previous episode' })).toBeInTheDocument()
    expect(within(nav).getByRole('button', { name: 'Next episode 2' })).toBeInTheDocument()

    // The two skip buttons are glyphs now too, and say so.
    const back = screen.getByRole('button', { name: 'Back 10 seconds' })
    expect(back).toHaveAttribute('title', 'Back 10 seconds')
    const forward = screen.getByRole('button', { name: 'Forward 10 seconds' })
    expect(forward).toHaveAttribute('title', 'Forward 10 seconds')
  })

  /**
   * The row's shape, not just its contents (owner, M15 second pass): transport
   * on the left, what-to-watch centred, fullscreen alone on the right. Asserted
   * as three sibling groups in document order, because the order is the point —
   * fullscreen moved out of the left group and to the far right, and a test
   * that only names the buttons would not have noticed.
   */
  it('lays the controls out in three groups, in order', async () => {
    renderPlayer()

    await readyVideo()

    const bar = screen.getByRole('group', { name: 'Player controls' })
    // The bar is [scrubber row, control row]; the control row is the second.
    const row = bar.children[1]
    if (row === undefined) throw new Error('the control row is missing')

    // `querySelectorAll` rather than `getAllByRole`, because what is being
    // asserted is document order across two different roles (a disabled
    // neighbour is a button, a ready one is a link).
    const groups = Array.from(row.children).map((group) =>
      Array.from(group.querySelectorAll('button, a')).map(
        (node) => node.getAttribute('aria-label') ?? '',
      ),
    )

    expect(groups).toEqual([
      ['Back 10 seconds', 'Play or pause', 'Forward 10 seconds'],
      ['Previous episode', 'Next episode 2', 'Unmark watched'],
      ['Fullscreen'],
    ])

    // The nav wraps exactly the two episode links and lives in the middle group.
    const nav = screen.getByRole('navigation', { name: 'Episodes' })
    expect(row.children[1]).toContainElement(nav)
    expect(within(nav).getAllByRole('button')).toHaveLength(2)
  })

  it('says what the episode is, beside the show it belongs to', async () => {
    renderPlayer()

    expect(
      await screen.findByRole('heading', { name: FRIEREN.title.preferred }),
    ).toBeInTheDocument()
    expect(screen.getByText(/The Journey’s End · subs en, audio ja/)).toBeInTheDocument()
  })
})

/**
 * The picture itself is a control (M15 sign-off): click to play or pause,
 * double-click for fullscreen. The whole difficulty is that a double click is
 * two clicks, so the single one has to be held back long enough to find out
 * whether it was the first half of one — otherwise going fullscreen would also
 * stop the episode, at exactly the moment nobody wants it stopped.
 */
describe('Player video gestures', () => {
  function controlBar(): HTMLElement | null {
    return screen.queryByRole('group', { name: 'Player controls' })
  }

  it('plays on a single click of the video, once the double-click window passes', async () => {
    stubFullscreen()
    renderPlayer()

    const video = await readyVideo()

    vi.useFakeTimers()
    try {
      act(() => {
        fireEvent.click(video)
      })
      // Nothing yet: the click is still waiting to be contradicted.
      expect(video.paused).toBe(true)

      act(() => {
        vi.advanceTimersByTime(300)
      })
      expect(video.paused).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('goes fullscreen on a double click, and does not pause as well', async () => {
    const { request } = stubFullscreen()
    renderPlayer()

    const video = await readyVideo()

    vi.useFakeTimers()
    try {
      act(() => {
        fireEvent.click(video)
      })
      act(() => {
        vi.advanceTimersByTime(120)
      })
      act(() => {
        fireEvent.click(video)
      })
      expect(request).toHaveBeenCalledTimes(1)

      // The pending single click was cancelled, so playback never moved.
      act(() => {
        vi.advanceTimersByTime(600)
      })
      expect(video.paused).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('toggles fullscreen from the bar and renames the button when it lands', async () => {
    const { request, exit } = stubFullscreen()
    renderPlayer()

    await readyVideo()

    await userEvent.click(screen.getByRole('button', { name: 'Fullscreen' }))
    expect(request).toHaveBeenCalledTimes(1)

    // The browser owns the state; the page reads it back off the document.
    act(() => {
      enterFullscreen()
    })
    const leave = await screen.findByRole('button', { name: 'Exit fullscreen' })
    expect(leave).toHaveAttribute('aria-pressed', 'true')

    await userEvent.click(leave)
    expect(exit).toHaveBeenCalledTimes(1)
  })

  /**
   * On a touch screen the first tap is how the controls come back, and it has
   * to mean only that: pausing as well would make the bar impossible to
   * consult without interrupting the episode.
   */
  it('lets a touch tap bring the hidden controls back without pausing', async () => {
    renderPlayer()

    const video = await readyVideo()

    vi.useFakeTimers()
    try {
      act(() => {
        void video.play()
      })
      act(() => {
        vi.advanceTimersByTime(2200)
      })
      expect(controlBar()).toBeNull()

      act(() => {
        fireEvent.touchStart(window)
        fireEvent.click(video)
      })
      expect(controlBar()).toBeInTheDocument()
      act(() => {
        vi.advanceTimersByTime(400)
      })
      expect(video.paused).toBe(false)

      // The next tap, with the bar already up, is an ordinary one.
      act(() => {
        fireEvent.touchStart(window)
        fireEvent.click(video)
      })
      act(() => {
        vi.advanceTimersByTime(400)
      })
      expect(video.paused).toBe(true)
    } finally {
      vi.useRealTimers()
    }
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

  /**
   * The strip carries the only Dismiss there is, and it sits inside the chrome
   * that the idle timer fades. While the warning is up the bar stays, whatever
   * the pointer is doing (owner, 2026-09-12).
   */
  it('holds the control bar open while the warning is up', async () => {
    const { fetchMock } = failingPlayer()
    const video = await readyVideo()

    await failWrites(video, fetchMock, [100, 200, 300])
    await screen.findByText(PROGRESS_UNSAVED)

    vi.useFakeTimers()
    try {
      act(() => {
        vi.advanceTimersByTime(10_000)
      })
      expect(screen.getByRole('group', { name: 'Player controls' })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
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
