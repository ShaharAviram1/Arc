import { QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DEMAND_NOT_FOUND_RETRIES,
  PENDING_POLL_MS,
  type EpisodeRef,
  type PlayInfo,
  type SequelListState,
} from '@/lib/playback'
import { createQueryClient } from '@/lib/queryClient'
import { Player } from '@/pages/Player'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import {
  callTo,
  jsonBodyOf,
  mockApi,
  requestsMade,
  TEST_USER,
  type MockRoutes,
} from '@/test/apiMock'
import { instances, resetHls } from '@/test/hlsMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

/**
 * Play on demand in the player (FR-A14, FR-S5 item 2, M20 T5): the demand,
 * the pending view that polls `/play` every 3 s ("Searching", "Downloading
 * 43 %", "Preparing") until the playlist arrives, the server's sentence on a
 * 409 with no spinner, and the end overlay's Next on a fetchable episode.
 */

const EPISODE = 9002
const PLAY = `GET /api/episodes/${String(EPISODE)}/play`
const DEMAND = `POST /api/episodes/${String(EPISODE)}/demand`
const PAUSED = 'Arc is paused by the admin, so nothing new can be fetched right now.'

function pendingInfo(state: string, progress: number | null, seeders: number | null): PlayInfo {
  return {
    ...PLAY_INFO_EPISODE_2,
    episode: { ...PLAY_INFO_EPISODE_2.episode, state },
    playlist_url: null,
    pending: { state, progress, seeders },
    subtitles: null,
  }
}

function renderAt(routes: MockRoutes, entry: { pathname: string; state?: unknown }) {
  // The routes object is read on every request, so a test can change an answer.
  const fetchMock = mockApi(routes)
  const router = createMemoryRouter(
    [
      { path: '/watch/:episodeId', element: <Player /> },
      { path: '/anime/:id', element: <p>show page</p> },
      { path: '/', element: <p>home</p> },
    ],
    { initialEntries: [entry] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { fetchMock, router }
}

function count(fetchMock: ReturnType<typeof mockApi>, request: string): number {
  return requestsMade(fetchMock).filter((made) => made === request).length
}

beforeEach(() => {
  resetHls()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('the pending player (FR-A14)', () => {
  it('demands, shows the download, polls every 3 s and plays once the playlist arrives', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const routes: MockRoutes = {
      'GET /api/auth/me': { body: TEST_USER },
      [DEMAND]: { status: 202, body: { state: 'downloading' } },
      [PLAY]: { body: pendingInfo('downloading', 0.43, 12) },
    }
    const { fetchMock } = renderAt(routes, {
      pathname: `/watch/${String(EPISODE)}`,
      state: { demand: true },
    })

    expect(await screen.findByText('Downloading 43 %')).toBeInTheDocument()
    expect(screen.getByText('12 seeders')).toBeInTheDocument()
    expect(screen.getByTestId('pending-spinner')).toBeInTheDocument()
    // The demand went first, once: `/play` would 404 for an episode nobody wants.
    const made = requestsMade(fetchMock)
    expect(made.indexOf(DEMAND)).toBeLessThan(made.indexOf(PLAY))
    expect(count(fetchMock, DEMAND)).toBe(1)
    expect(count(fetchMock, PLAY)).toBe(1)

    routes[PLAY] = { body: pendingInfo('preparing', null, 12) }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PENDING_POLL_MS)
    })
    expect(await screen.findByText('Preparing')).toBeInTheDocument()
    expect(count(fetchMock, PLAY)).toBe(2)

    routes[PLAY] = { body: PLAY_INFO_EPISODE_2 }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PENDING_POLL_MS)
    })
    // The playlist is here: the player proper, on hls.js.
    await waitFor(() => {
      expect(instances).toHaveLength(1)
    })
    expect(screen.queryByTestId('pending-spinner')).not.toBeInTheDocument()

    // And the poll stops.
    const polls = count(fetchMock, PLAY)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PENDING_POLL_MS * 3)
    })
    expect(count(fetchMock, PLAY)).toBe(polls)
    expect(count(fetchMock, DEMAND)).toBe(1)
  })

  it('says "Searching" before there is a release', async () => {
    renderAt(
      {
        'GET /api/auth/me': { body: TEST_USER },
        [DEMAND]: { status: 202, body: { state: 'wanted' } },
        [PLAY]: { body: pendingInfo('wanted', null, null) },
      },
      { pathname: `/watch/${String(EPISODE)}`, state: { demand: true } },
    )

    expect(await screen.findByText('Searching')).toBeInTheDocument()
    expect(screen.queryByText(/seeders/)).not.toBeInTheDocument()
  })

  it('shows the server’s sentence on a 409, with no spinner and no poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { fetchMock } = renderAt(
      {
        'GET /api/auth/me': { body: TEST_USER },
        [DEMAND]: {
          status: 409,
          body: { detail: { code: 'acquisition_paused', message: PAUSED } },
        },
      },
      { pathname: `/watch/${String(EPISODE)}`, state: { demand: true } },
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(PAUSED)
    expect(screen.queryByTestId('pending-spinner')).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    const polls = count(fetchMock, PLAY)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PENDING_POLL_MS * 3)
    })
    expect(count(fetchMock, PLAY)).toBe(polls)
    expect(screen.getByRole('alert')).toHaveTextContent(PAUSED)
  })

  it('stops polling once a pending answer turns into a 404', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const routes: MockRoutes = {
      'GET /api/auth/me': { body: TEST_USER },
      [DEMAND]: { status: 202, body: { state: 'downloading' } },
      [PLAY]: { body: pendingInfo('downloading', 0.5, 3) },
    }
    const { fetchMock } = renderAt(routes, {
      pathname: `/watch/${String(EPISODE)}`,
      state: { demand: true },
    })
    expect(await screen.findByText('Downloading 50 %')).toBeInTheDocument()

    // The want went away (the user watched past it elsewhere).
    routes[PLAY] = { status: 404, body: { detail: 'not playable' } }
    for (let tick = 0; tick < 6; tick += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(PENDING_POLL_MS)
      })
    }
    expect(await screen.findByText('Episode not ready')).toBeInTheDocument()

    const polls = count(fetchMock, PLAY)
    for (let tick = 0; tick < 5; tick += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(PENDING_POLL_MS)
      })
    }
    expect(count(fetchMock, PLAY)).toBe(polls)
  })

  it('asks through a 404 right after an accepted demand (the reconciler race)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const routes: MockRoutes = {
      'GET /api/auth/me': { body: TEST_USER },
      [DEMAND]: { status: 202, body: { state: 'wanted' } },
      [PLAY]: { status: 404, body: { detail: 'not playable' } },
    }
    const { fetchMock } = renderAt(routes, {
      pathname: `/watch/${String(EPISODE)}`,
      state: { demand: true },
    })
    await waitFor(() => {
      expect(count(fetchMock, PLAY)).toBe(1)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PENDING_POLL_MS)
    })
    expect(count(fetchMock, PLAY)).toBe(2)
    expect(screen.queryByText('Episode not ready')).not.toBeInTheDocument()

    routes[PLAY] = { body: pendingInfo('wanted', null, null) }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PENDING_POLL_MS)
    })
    expect(await screen.findByText('Searching')).toBeInTheDocument()
  })

  it('takes the 404 after three more tries following an accepted demand', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { fetchMock } = renderAt(
      {
        'GET /api/auth/me': { body: TEST_USER },
        [DEMAND]: { status: 202, body: { state: 'wanted' } },
        [PLAY]: { status: 404, body: { detail: 'not playable' } },
      },
      { pathname: `/watch/${String(EPISODE)}`, state: { demand: true } },
    )
    for (let tick = 0; tick < 6; tick += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(PENDING_POLL_MS)
      })
    }

    expect(await screen.findByText('Episode not ready')).toBeInTheDocument()
    expect(count(fetchMock, PLAY)).toBe(1 + DEMAND_NOT_FOUND_RETRIES)
  })

  it('opened on an episode already pending, demands it once and waits', async () => {
    const { fetchMock } = renderAt(
      {
        'GET /api/auth/me': { body: TEST_USER },
        [DEMAND]: { status: 202, body: { state: 'downloading' } },
        [PLAY]: { body: pendingInfo('downloading', 0.1, 4) },
      },
      { pathname: `/watch/${String(EPISODE)}` },
    )

    expect(await screen.findByText('Downloading 10 %')).toBeInTheDocument()
    await waitFor(() => {
      expect(count(fetchMock, DEMAND)).toBe(1)
    })
  })
})

describe('the end overlay’s Next on a fetchable episode (FR-S5 item 2)', () => {
  const CURRENT = `GET /api/episodes/9001/play`

  async function endEpisode() {
    await waitFor(() => {
      expect(instances).toHaveLength(1)
    })
    const video = document.querySelector('video')
    if (video === null) throw new Error('no video element rendered')
    fireEvent.ended(video)
  }

  it('offers Next when the next episode can be fetched, demands, then navigates', async () => {
    const { fetchMock, router } = renderAt(
      {
        'GET /api/auth/me': { body: TEST_USER },
        'POST /api/progress': {
          body: { completed: false, newly_completed: false, list_progress: null },
        },
        [CURRENT]: {
          body: {
            ...PLAY_INFO,
            next: { id: EPISODE, number: 2, state: 'not_wanted', ready: false, fetchable: true },
          },
        },
        [DEMAND]: { status: 202, body: { state: 'wanted' } },
        [PLAY]: { body: pendingInfo('wanted', null, null) },
      },
      { pathname: '/watch/9001' },
    )
    await endEpisode()

    expect(await screen.findByText('Next: Episode 2')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Next episode' }))

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/watch/${String(EPISODE)}`)
    })
    expect(await screen.findByText('Searching')).toBeInTheDocument()
    // Demanded by the overlay; the player does not ask a second time.
    expect(count(fetchMock, DEMAND)).toBe(1)
  })

  it('does not offer Next when the next episode is neither ready nor fetchable', async () => {
    renderAt(
      {
        'GET /api/auth/me': { body: TEST_USER },
        'POST /api/progress': {
          body: { completed: false, newly_completed: false, list_progress: null },
        },
        [CURRENT]: { body: PLAY_INFO },
      },
      { pathname: '/watch/9001' },
    )
    await endEpisode()

    expect(await screen.findByText('Episode 2 isn’t ready yet')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next episode' })).not.toBeInTheDocument()
  })

  it('says the server’s sentence in the overlay when the demand is refused', async () => {
    const { router } = renderAt(
      {
        'GET /api/auth/me': { body: TEST_USER },
        'POST /api/progress': {
          body: { completed: false, newly_completed: false, list_progress: null },
        },
        [CURRENT]: {
          body: {
            ...PLAY_INFO,
            next: { id: EPISODE, number: 2, state: 'not_wanted', ready: false, fetchable: true },
          },
        },
        [DEMAND]: {
          status: 409,
          body: { detail: { code: 'storage_held', message: 'Storage is full.' } },
        },
      },
      { pathname: '/watch/9001' },
    )
    await endEpisode()

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Next episode' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Storage is full.')
    expect(router.state.location.pathname).toBe('/watch/9001')
    expect(screen.queryByRole('button', { name: 'Next episode' })).not.toBeInTheDocument()
  })
})

describe('Next season at a finished season’s end (FR-S5 item 3)', () => {
  const CURRENT = `GET /api/episodes/9001/play`
  const SEQUEL_EPISODE = 9101
  const SEQUEL_PLAY = `GET /api/episodes/${String(SEQUEL_EPISODE)}/play`
  const SEQUEL_DEMAND = `POST /api/episodes/${String(SEQUEL_EPISODE)}/demand`
  const LIST_PUT = 'PUT /api/list/77'
  const HEADING = 'Next season: Iruma-kun 3 · Episode 1'
  const NOT_READY: Partial<EpisodeRef> = { state: 'not_wanted', ready: false, fetchable: true }

  function lastEpisode(listState: SequelListState, episode: Partial<EpisodeRef> = {}): PlayInfo {
    return {
      ...PLAY_INFO,
      next: null,
      next_season: {
        anime_id: 77,
        title: 'Iruma-kun 3',
        list_state: listState,
        episode: {
          id: SEQUEL_EPISODE,
          number: 1,
          state: 'ready',
          ready: true,
          fetchable: false,
          ...episode,
        },
      },
    }
  }

  function routesFor(info: PlayInfo): MockRoutes {
    return {
      'GET /api/auth/me': { body: TEST_USER },
      'POST /api/progress': {
        body: { completed: false, newly_completed: false, list_progress: null },
      },
      [CURRENT]: { body: info },
      [LIST_PUT]: {
        body: {
          anime_id: 77,
          status: 'watching',
          progress: 0,
          score: null,
          updated_at: '2026-10-18T10:00:00Z',
          dormant: false,
        },
      },
      [SEQUEL_DEMAND]: { status: 202, body: { state: 'wanted' } },
      [SEQUEL_PLAY]: {
        body:
          (info.next_season?.episode.ready ?? false)
            ? {
                ...PLAY_INFO_EPISODE_2,
                episode: { ...PLAY_INFO_EPISODE_2.episode, id: SEQUEL_EPISODE },
              }
            : pendingInfo('wanted', null, null),
      },
    }
  }

  async function endEpisode() {
    await waitFor(() => {
      expect(instances).toHaveLength(1)
    })
    const video = document.querySelector('video')
    if (video === null) throw new Error('no video element rendered')
    fireEvent.ended(video)
  }

  it('offers the sequel’s episode 1 when the show has no next episode', async () => {
    const { fetchMock, router } = renderAt(routesFor(lastEpisode('active')), {
      pathname: '/watch/9001',
    })
    await endEpisode()

    expect(await screen.findByText(HEADING)).toBeInTheDocument()
    expect(screen.queryByText('This was the last episode')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep watching' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to the show' })).toBeInTheDocument()

    // Active and ready: a plain link, no list write and no demand.
    await userEvent.setup().click(screen.getByRole('link', { name: 'Next season' }))
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/watch/${String(SEQUEL_EPISODE)}`)
    })
    expect(router.state.location.state).toBeNull()
    expect(count(fetchMock, LIST_PUT)).toBe(0)
    expect(count(fetchMock, SEQUEL_DEMAND)).toBe(0)
  })

  it('activates a dormant sequel, demands episode 1, then opens the waiting player', async () => {
    const { fetchMock, router } = renderAt(routesFor(lastEpisode('dormant', NOT_READY)), {
      pathname: '/watch/9001',
    })
    await endEpisode()

    expect(await screen.findByText(HEADING)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Next season' }))

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/watch/${String(SEQUEL_EPISODE)}`)
    })
    expect(router.state.location.state).toEqual({ demanded: true })
    expect(await screen.findByText('Searching')).toBeInTheDocument()
    const made = requestsMade(fetchMock)
    expect(made.indexOf(LIST_PUT)).toBeLessThan(made.indexOf(SEQUEL_DEMAND))
    // The touch re-sends the entry unchanged, as "Fetch this show" does.
    expect(jsonBodyOf(callTo(fetchMock, '/api/list/77'))).toEqual({})
    expect(count(fetchMock, SEQUEL_DEMAND)).toBe(1)
  })

  it('adds an absent sequel to the list as Watching before demanding', async () => {
    const { fetchMock, router } = renderAt(routesFor(lastEpisode('absent', NOT_READY)), {
      pathname: '/watch/9001',
    })
    await endEpisode()

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Next season' }))

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/watch/${String(SEQUEL_EPISODE)}`)
    })
    expect(jsonBodyOf(callTo(fetchMock, '/api/list/77'))).toEqual({ status: 'watching' })
    expect(count(fetchMock, SEQUEL_DEMAND)).toBe(1)
  })

  it('adds an absent, ready sequel and opens it without a demand', async () => {
    const { fetchMock, router } = renderAt(routesFor(lastEpisode('absent')), {
      pathname: '/watch/9001',
    })
    await endEpisode()

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Next season' }))

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/watch/${String(SEQUEL_EPISODE)}`)
    })
    expect(count(fetchMock, LIST_PUT)).toBe(1)
    expect(count(fetchMock, SEQUEL_DEMAND)).toBe(0)
  })

  it('says the server’s sentence when the demand is refused', async () => {
    const routes = routesFor(lastEpisode('dormant', NOT_READY))
    routes[SEQUEL_DEMAND] = {
      status: 409,
      body: { detail: { code: 'acquisition_paused', message: PAUSED } },
    }
    const { router } = renderAt(routes, { pathname: '/watch/9001' })
    await endEpisode()

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Next season' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(PAUSED)
    expect(router.state.location.pathname).toBe('/watch/9001')
    expect(screen.queryByRole('button', { name: 'Next season' })).not.toBeInTheDocument()
  })

  it('says the list’s sentence when the list write fails, and neither demands nor leaves', async () => {
    const routes = routesFor(lastEpisode('absent', NOT_READY))
    routes[LIST_PUT] = { status: 500, body: { detail: 'boom' } }
    const { fetchMock, router } = renderAt(routes, { pathname: '/watch/9001' })
    await endEpisode()

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Next season' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not save that change. Try again.',
    )
    expect(count(fetchMock, LIST_PUT)).toBe(1)
    expect(count(fetchMock, SEQUEL_DEMAND)).toBe(0)
    expect(router.state.location.pathname).toBe('/watch/9001')
  })

  it('shares one press between the overlay and the glyph', async () => {
    const routes = routesFor(lastEpisode('dormant', NOT_READY))
    routes[SEQUEL_DEMAND] = {
      status: 409,
      body: { detail: { code: 'acquisition_paused', message: PAUSED } },
    }
    const { fetchMock } = renderAt(routes, { pathname: '/watch/9001' })
    await endEpisode()

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Next season' }))

    // The overlay says it once; the glyph knows it too and offers no second press.
    expect(await screen.findByRole('alert')).toHaveTextContent(PAUSED)
    const glyph = screen.getByRole('button', { name: PAUSED })
    expect(glyph).toBeDisabled()
    expect(glyph).toHaveAttribute('title', PAUSED)
    expect(count(fetchMock, LIST_PUT)).toBe(1)
    expect(count(fetchMock, SEQUEL_DEMAND)).toBe(1)
  })

  it('says a sequel that has not aired yet has not aired, on the overlay and the glyph', async () => {
    const info = lastEpisode('absent', { state: 'not_wanted', ready: false, fetchable: false })
    if (info.next_season == null) throw new Error('no sequel in the fixture')
    info.next_season.aired = false
    const { fetchMock } = renderAt(routesFor(info), { pathname: '/watch/9001' })

    const unaired = 'Iruma-kun 3 · Episode 1 hasn’t aired yet'
    const glyph = await screen.findByRole('button', { name: unaired })
    expect(glyph).toBeDisabled()
    expect(glyph).toHaveAttribute('title', unaired)

    await endEpisode()
    expect(await screen.findByText(unaired)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next season' })).not.toBeInTheDocument()
    expect(count(fetchMock, LIST_PUT)).toBe(0)
  })

  it('gives a disabled glyph the same label and tooltip', async () => {
    renderAt(
      routesFor(lastEpisode('active', { state: 'not_wanted', ready: false, fetchable: false })),
      { pathname: '/watch/9001' },
    )

    const notReady = 'Iruma-kun 3 · Episode 1 isn’t ready yet'
    const glyph = await screen.findByRole('button', { name: notReady })
    expect(glyph).toBeDisabled()
    expect(glyph).toHaveAttribute('title', notReady)
  })

  it('names the sequel without offering it when it can be neither played nor fetched', async () => {
    renderAt(
      routesFor(lastEpisode('active', { state: 'not_wanted', ready: false, fetchable: false })),
      { pathname: '/watch/9001' },
    )
    await endEpisode()

    expect(await screen.findByText('Iruma-kun 3 · Episode 1 isn’t ready yet')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next season' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Next season' })).not.toBeInTheDocument()
  })

  it('points the Next glyph at the sequel, under a Next season label', async () => {
    renderAt(routesFor(lastEpisode('active')), { pathname: '/watch/9001' })

    const glyph = await screen.findByRole('link', { name: HEADING })
    expect(glyph).toHaveAttribute('href', `/watch/${String(SEQUEL_EPISODE)}`)
    expect(screen.queryByRole('button', { name: 'Next episode' })).not.toBeInTheDocument()
  })

  it('runs the same touch and demand from the glyph', async () => {
    const { fetchMock, router } = renderAt(routesFor(lastEpisode('dormant', NOT_READY)), {
      pathname: '/watch/9001',
    })

    await userEvent.setup().click(await screen.findByRole('button', { name: HEADING }))

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/watch/${String(SEQUEL_EPISODE)}`)
    })
    expect(count(fetchMock, LIST_PUT)).toBe(1)
    expect(count(fetchMock, SEQUEL_DEMAND)).toBe(1)
  })
})
