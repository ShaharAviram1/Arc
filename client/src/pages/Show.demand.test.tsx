import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation, useParams } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnimeDetail } from '@/lib/anime'
import { wantsDemand } from '@/lib/playback'
import { createQueryClient } from '@/lib/queryClient'
import { Show } from '@/pages/Show'
import { FRIEREN, FRIEREN_DETAIL } from '@/test/animeFixtures'
import { mockApi, TEST_USER } from '@/test/apiMock'

/**
 * Play on demand from the show page (FR-A14, M20 T5): a window episode that
 * is not ready but `fetchable` gets a Play button that opens the player with
 * the demand flag; a ready one and one the window does not cover get none.
 */

const DETAIL = `GET /api/anime/${String(FRIEREN.id)}`

/** Episode 4 (9004) is `downloading` in the fixture; 9001 is ready. */
function detail(fetchable: Record<number, boolean>): AnimeDetail {
  return {
    ...FRIEREN_DETAIL,
    episodes: FRIEREN_DETAIL.episodes.map((episode) => ({
      ...episode,
      fetchable: fetchable[episode.id] ?? false,
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

describe('Show — play on demand (FR-A14)', () => {
  it('offers Play on a fetchable episode and opens the player with the demand', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({ 9004: true }) },
    })

    renderShow()
    const play = await screen.findByRole('link', { name: 'Fetch and play episode 4' })
    await userEvent.setup().click(play)

    expect(await screen.findByText('watching 9004 (demand)')).toBeInTheDocument()
  })

  it('offers no Play button on a ready episode', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      // Even a stray flag on a ready row draws nothing: it streams.
      [DETAIL]: { body: detail({ 9001: true }) },
    })

    renderShow()
    await screen.findByRole('link', { name: /^1\. / })
    expect(screen.queryByRole('link', { name: 'Fetch and play episode 1' })).not.toBeInTheDocument()
  })

  it('offers none on a not-ready episode the window does not cover', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({}) },
    })

    renderShow()
    await screen.findByRole('link', { name: /^1\. / })
    expect(screen.queryByRole('link', { name: /^Fetch and play/ })).not.toBeInTheDocument()
    // And the row itself is not a target.
    expect(screen.queryByRole('link', { name: /^4\. / })).not.toBeInTheDocument()
  })
})
