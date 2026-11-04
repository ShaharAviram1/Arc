import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation, useParams } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnimeDetail, EpisodeOut, Rewatch, ShowDemand } from '@/lib/anime'
import { wantsDemand } from '@/lib/playback'
import { createQueryClient } from '@/lib/queryClient'
import { Show } from '@/pages/Show'
import { FRIEREN, FRIEREN_DETAIL } from '@/test/animeFixtures'
import { mockApi, requestsMade, TEST_USER, type MockRoutes } from '@/test/apiMock'

/**
 * Play on any episode from the show page (FR-A14, M21 T6), and the rewatch
 * switch (FR-W7). Every aired episode is a target: a not-ready one opens the
 * player with the demand flag and has a Play chip; an un-aired one is inert;
 * a demanded one says "Fetching on request" with a ✕; the page's refusal is
 * said in place of the navigation.
 */

const DETAIL = `GET /api/anime/${String(FRIEREN.id)}`
const REWATCH = `/api/anime/${String(FRIEREN.id)}/rewatch`
const HINT =
  'Arc fetches it now; it plays as it downloads, or when the file is complete for a season pack.'

/**
 * The fixture as the M21 server sends it: every aired, not-ready episode is
 * `fetchable` (9002 preparing, 9004 downloading, 9005 unavailable, 9006
 * searching, 9007 failed); 9001 is ready; 9003 has not aired.
 */
function detail(
  overrides: Partial<AnimeDetail> = {},
  episodes: Record<number, Partial<EpisodeOut>> = {},
): AnimeDetail {
  return {
    ...FRIEREN_DETAIL,
    ...overrides,
    episodes: FRIEREN_DETAIL.episodes.map((episode) => ({
      ...episode,
      fetchable: episode.aired && episode.state !== 'ready',
      ...episodes[episode.id],
    })),
  }
}

function WatchProbe() {
  const { episodeId } = useParams()
  const location = useLocation()
  return <p>{`watching ${episodeId ?? ''}${wantsDemand(location.state) ? ' (demand)' : ''}`}</p>
}

function renderShow() {
  const router = createMemoryRouter(
    [
      { path: '/anime/:id', element: <Show /> },
      { path: '/watch/:episodeId', element: <WatchProbe /> },
    ],
    { initialEntries: [`/anime/${String(FRIEREN.id)}`] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('EpisodeRow — play any aired episode (FR-A14, M21)', () => {
  it('aired not-ready row is a link and shows the Play chip with the hint', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, [DETAIL]: { body: detail() } })

    renderShow()
    const chip = await screen.findByRole('link', { name: 'Fetch and play episode 4' })
    expect(chip).toHaveAttribute('title', HINT)
    // An unavailable and a failed row are offered too: any aired episode.
    expect(screen.getByRole('link', { name: 'Fetch and play episode 5' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Fetch and play episode 7' })).toBeInTheDocument()

    // The row's own title is the link too, with the demand.
    await userEvent.setup().click(screen.getByRole('link', { name: /^4\. / }))
    expect(await screen.findByText('watching 9004 (demand)')).toBeInTheDocument()
  })

  it('un-aired row is inert', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, [DETAIL]: { body: detail() } })

    renderShow()
    await screen.findByRole('link', { name: /^1\. / })
    expect(screen.queryByRole('link', { name: /^3\. / })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Fetch and play episode 3' })).not.toBeInTheDocument()
  })

  it('demanded row shows "Fetching on request" and its ✕ calls withdraw', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({}, { 9004: { demanded: true } }) },
      'DELETE /api/episodes/9004/demand': { status: 204 },
    })

    renderShow()
    expect(await screen.findByText('Fetching on request')).toBeInTheDocument()
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: 'Withdraw the request for episode 4' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('DELETE /api/episodes/9004/demand')
    })
    // And the page asks again, so the row follows the server.
    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((request) => request === DETAIL).length).toBe(2)
    })
  })

  it('a ready demanded row shows "Requested" and its ✕ withdraws', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({}, { 9001: { demanded: true } }) },
      'DELETE /api/episodes/9001/demand': { status: 204 },
    })

    renderShow()
    expect(await screen.findByText('Requested')).toBeInTheDocument()
    expect(screen.queryByText('Fetching on request')).not.toBeInTheDocument()
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: 'Withdraw the request for episode 1' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('DELETE /api/episodes/9001/demand')
    })
  })

  it('says so when the withdraw fails', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({}, { 9004: { demanded: true } }) },
      'DELETE /api/episodes/9004/demand': { status: 500, body: { detail: 'boom' } },
    })

    renderShow()
    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: 'Withdraw the request for episode 4' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not withdraw that. Try again.',
    )
  })

  it('the cap list’s ✕ withdraws and the list shrinks', async () => {
    const demand: ShowDemand = {
      open: [
        { episode_id: 7001, anime_id: 77, title: 'Dungeon Meshi', number: 3 },
        { episode_id: 8002, anime_id: 88, title: 'Mushishi', number: 12 },
      ],
      max: 2,
      refusal: { code: 'demand_cap', message: 'You already have 2 episodes on request.' },
    }
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({ demand }) },
      'DELETE /api/episodes/7001/demand': { status: 204 },
    })

    renderShow()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('link', { name: 'Fetch and play episode 4' }))
    await user.click(
      await screen.findByRole('button', {
        name: 'Withdraw the request for Dungeon Meshi episode 3',
      }),
    )

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('DELETE /api/episodes/7001/demand')
    })
    await waitFor(() => {
      expect(screen.queryByRole('link', { name: 'Dungeon Meshi' })).not.toBeInTheDocument()
    })
    expect(screen.getByRole('link', { name: 'Mushishi' })).toBeInTheDocument()
  })

  it('a page refusal shows the toast and does not navigate', async () => {
    const demand: ShowDemand = {
      open: [{ episode_id: 7001, anime_id: 77, title: 'Dungeon Meshi', number: 3 }],
      max: 1,
      refusal: { code: 'demand_cap', message: 'You already have 1 episode on request.' },
    }
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, [DETAIL]: { body: detail({ demand }) } })

    renderShow()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('link', { name: 'Fetch and play episode 4' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('You already have 1 episode on request.')
    // The cap lists the open demands, each a link.
    expect(within(alert).getByRole('link', { name: 'Dungeon Meshi' })).toHaveAttribute(
      'href',
      '/anime/77',
    )
    expect(within(alert).getByRole('link', { name: 'Episode 3' })).toHaveAttribute(
      'href',
      '/watch/7001',
    )
    expect(screen.queryByText(/^watching/)).not.toBeInTheDocument()
  })

  it('the cap does not stop a row that is already demanded', async () => {
    const demand: ShowDemand = {
      open: [{ episode_id: 9004, anime_id: FRIEREN.id, title: 'Frieren', number: 4 }],
      max: 1,
      refusal: { code: 'demand_cap', message: 'You already have 1 episode on request.' },
    }
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({ demand }, { 9004: { demanded: true } }) },
    })

    renderShow()
    await userEvent
      .setup()
      .click(await screen.findByRole('link', { name: 'Fetch and play episode 4' }))
    expect(await screen.findByText('watching 9004 (demand)')).toBeInTheDocument()
  })

  it('a ready row plays as before', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, [DETAIL]: { body: detail() } })

    renderShow()
    const row = await screen.findByRole('link', { name: /^1\. / })
    expect(screen.queryByRole('link', { name: 'Fetch and play episode 1' })).not.toBeInTheDocument()
    await userEvent.setup().click(row)
    expect(await screen.findByText('watching 9001')).toBeInTheDocument()
  })
})

describe('Hero — play and rewatch (M21)', () => {
  it('offers a not-ready first unwatched episode with the demand', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, [DETAIL]: { body: detail() } })

    renderShow()
    await userEvent.setup().click(await screen.findByRole('link', { name: 'Play episode 2' }))
    expect(await screen.findByText('watching 9002 (demand)')).toBeInTheDocument()
  })

  it('has no "Try episode 1" any more (owner decision 1)', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, [DETAIL]: { body: detail() } })

    renderShow()
    await screen.findByRole('link', { name: /^1\. / })
    expect(screen.queryByRole('button', { name: /^Try episode/ })).not.toBeInTheDocument()
  })

  const PASS: Rewatch = {
    started_at: '2026-11-04T09:00:00Z',
    position: 3,
    finished_in_pass: 3,
    total: 28,
  }

  it('RewatchControl starts a pass', async () => {
    const opened: Rewatch = { ...PASS, position: 0, finished_in_pass: 0 }
    const routes: MockRoutes = {
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail() },
      [`PUT ${REWATCH}`]: { body: opened },
    }
    const fetchMock = mockApi(routes)

    renderShow()
    const toggle = await screen.findByRole('button', { name: 'Rewatch' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    // The refetch after the PUT finds the pass open on the server.
    routes[DETAIL] = { body: detail({ rewatch: opened }) }
    await userEvent.setup().click(toggle)

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`PUT ${REWATCH}`)
    })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Rewatch' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
    })
    expect(await screen.findByText('Rewatching · ep 0 / 28')).toBeInTheDocument()
  })

  it('RewatchControl stops a pass', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({ rewatch: PASS }) },
      [`DELETE ${REWATCH}`]: { status: 204 },
    })

    renderShow()
    const toggle = await screen.findByRole('button', { name: 'Rewatch' })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    await userEvent.setup().click(toggle)

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`DELETE ${REWATCH}`)
    })
  })

  it('RewatchControl shows position/total and that the list is left alone', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({ rewatch: PASS }) },
    })

    renderShow()
    expect(await screen.findByText('Rewatching · ep 3 / 28')).toBeInTheDocument()
    expect(screen.getByText('Your list and MyAnimeList are left as they are.')).toBeInTheDocument()
  })

  it('RewatchControl shows the 409 message', async () => {
    const sentence = 'nothing of this show has aired yet'
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail() },
      [`PUT ${REWATCH}`]: { status: 409, body: { detail: sentence } },
    })

    renderShow()
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Rewatch' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(sentence)
  })
})
