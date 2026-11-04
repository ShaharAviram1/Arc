import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnimeDetail } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import { Show } from '@/pages/Show'
import { FRIEREN, FRIEREN_DETAIL } from '@/test/animeFixtures'
import { mockApi, TEST_ADMIN, TEST_USER } from '@/test/apiMock'

/**
 * An episode whose season pack waits for a person's file pick (M21, FR-A4):
 * its row links to the Review page's season-pack section.
 */

const DETAIL = `GET /api/anime/${String(FRIEREN.id)}`
const REASON = "the pack's files need a person's pick (Review)"
const LINK = 'Pick the file in Review'

function detail(): AnimeDetail {
  return {
    ...FRIEREN_DETAIL,
    episodes: FRIEREN_DETAIL.episodes.map((episode, index) =>
      index === 0
        ? { ...episode, state: 'unavailable', unavailable_reason: REASON, awaiting_pick: true }
        : episode,
    ),
  }
}

function renderShow() {
  const router = createMemoryRouter([{ path: '/anime/:id', element: <Show /> }], {
    initialEntries: [`/anime/${String(FRIEREN.id)}`],
  })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Show — a pack waiting for a pick (M21)', () => {
  it("links an admin's row to the season-pack section of Review", async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_ADMIN }, [DETAIL]: { body: detail() } })

    renderShow()

    expect(await screen.findByRole('link', { name: LINK })).toHaveAttribute(
      'href',
      '/review#season-packs',
    )
  })

  it('offers no link to a viewer who cannot pick', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, [DETAIL]: { body: detail() } })

    renderShow()

    expect(await screen.findAllByText('Unavailable')).not.toHaveLength(0)
    expect(screen.queryByRole('link', { name: LINK })).not.toBeInTheDocument()
  })
})
