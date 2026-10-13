import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PlayInfo } from '@/lib/playback'
import { createQueryClient } from '@/lib/queryClient'
import { Home } from '@/pages/Home'
import { Player } from '@/pages/Player'
import {
  CONTINUE_FRIEREN,
  EMPTY_SCHEDULE,
  HOME_PAGE_CONTINUE,
  PLAY_INFO_EPISODE_2,
  RECS_PAGE_EMPTY,
} from '@/test/animeFixtures'
import { mockApi, requestsMade, TEST_USER, type MockRoutes } from '@/test/apiMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

/**
 * Watch Now's episode shelves and play on demand (FR-A14, M20 T5): a
 * Continue-watching entry whose files are gone (deleted after watching) opens
 * the player with the demand, which fetches it and waits; a ready one opens
 * the player as before, with no demand.
 */

const HEALTH = { status: 'ok', version: '0.1.0', env: 'dev', tmdb_enabled: false }
const EPISODE = CONTINUE_FRIEREN.episode.id
const DEMAND = `POST /api/episodes/${String(EPISODE)}/demand`
const PLAY = `GET /api/episodes/${String(EPISODE)}/play`

const PENDING: PlayInfo = {
  ...PLAY_INFO_EPISODE_2,
  episode: { ...CONTINUE_FRIEREN.episode, state: 'searching' },
  playlist_url: null,
  pending: { state: 'searching', progress: null, seeders: null },
}

function renderHome(routes: MockRoutes) {
  const fetchMock = mockApi({
    'GET /api/auth/me': { body: TEST_USER },
    'GET /api/health': { body: HEALTH },
    'GET /api/schedule': { body: EMPTY_SCHEDULE },
    'GET /api/recs': { body: RECS_PAGE_EMPTY },
    'GET /api/list': { body: [] },
    ...routes,
  })
  const router = createMemoryRouter(
    [
      { path: '/', element: <Home /> },
      { path: '/watch/:episodeId', element: <Player /> },
    ],
    { initialEntries: ['/'] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return fetchMock
}

async function pressContinueTile(): Promise<void> {
  const heading = await screen.findByRole('heading', { level: 2, name: 'Continue watching' })
  const shelf = heading.closest('section')
  if (shelf === null) throw new Error('no shelf')
  const tile = within(shelf).getAllByRole('link')[0] as HTMLElement
  await userEvent.setup().click(tile)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Home — play on demand (FR-A14)', () => {
  it('demands a Continue-watching episode whose files are gone, then waits for it', async () => {
    const gone = { ...CONTINUE_FRIEREN.episode, state: 'not_wanted' as const, rendition: null }
    const fetchMock = renderHome({
      'GET /api/home': {
        body: {
          ...HOME_PAGE_CONTINUE,
          continue_watching: [{ ...CONTINUE_FRIEREN, episode: gone }],
        },
      },
      [DEMAND]: { status: 202, body: { state: 'wanted' } },
      [PLAY]: { body: PENDING },
    })

    await pressContinueTile()

    expect(await screen.findByText('Searching')).toBeInTheDocument()
    const made = requestsMade(fetchMock)
    expect(made).toContain(DEMAND)
    expect(made.indexOf(DEMAND)).toBeLessThan(made.indexOf(PLAY))
  })

  it('does not demand a ready Continue-watching episode', async () => {
    const fetchMock = renderHome({
      'GET /api/home': { body: HOME_PAGE_CONTINUE },
      [PLAY]: { body: { ...PLAY_INFO_EPISODE_2, episode: CONTINUE_FRIEREN.episode } },
    })

    await pressContinueTile()

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(PLAY)
    })
    expect(requestsMade(fetchMock)).not.toContain(DEMAND)
  })
})
