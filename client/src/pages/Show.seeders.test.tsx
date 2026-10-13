import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnimeDetail } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import { Show } from '@/pages/Show'
import { FRIEREN, FRIEREN_DETAIL } from '@/test/animeFixtures'
import { mockApi, requestsMade, TEST_DEMO_USER, TEST_USER } from '@/test/apiMock'

/**
 * The few-seeders line on an episode row (FR-A16, M20 T9): "Few seeders (2) —
 * pin this show to keep it", with a Pin action that is the hero's "Keep files"
 * mutation (FR-T8).
 */

const DETAIL = `GET /api/anime/${String(FRIEREN.id)}`
const PIN_PATH = `/api/anime/${String(FRIEREN.id)}/pin`
const LINE = 'Few seeders (2) — pin this show to keep it'

function detail(overrides: { few_seeders?: boolean; pinned?: boolean } = {}): AnimeDetail {
  return {
    ...FRIEREN_DETAIL,
    pinned: overrides.pinned ?? false,
    episodes: FRIEREN_DETAIL.episodes.map((episode, index) =>
      index === 0
        ? { ...episode, few_seeders: overrides.few_seeders ?? true, seeders: 2 }
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

describe('Show — few seeders (FR-A16)', () => {
  it('says so on the row and pins the show from there', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail() },
      [`PUT ${PIN_PATH}`]: { status: 204 },
    })

    renderShow()
    expect(await screen.findByText(LINE)).toBeInTheDocument()
    const pin = screen.getByRole('button', { name: 'Pin this show' })
    // The why, for screen readers and touch (not only a `title`).
    expect(pin).toHaveAccessibleDescription(/pinning the show keeps its files/)
    await userEvent.setup().click(pin)

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`PUT ${PIN_PATH}`)
    })
    // Pinned: the count stays, the suggestion and its button go.
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Pin this show' })).not.toBeInTheDocument()
    })
    expect(screen.getByText('Few seeders (2)')).toBeInTheDocument()
  })

  it('drops the suggestion on a show already pinned', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({ pinned: true }) },
    })

    renderShow()
    expect(await screen.findByText('Few seeders (2)')).toBeInTheDocument()
    expect(screen.queryByText(LINE)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Pin this show' })).not.toBeInTheDocument()
  })

  it('shows nothing when the episode has enough seeders', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail({ few_seeders: false }) },
    })

    renderShow()
    await screen.findByRole('heading', { level: 1 })
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('GET /api/auth/me')
    })
    expect(screen.queryByText(/Few seeders/)).not.toBeInTheDocument()
  })

  it('offers no Pin to the demo account', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_DEMO_USER },
      [DETAIL]: { body: detail() },
    })

    renderShow()
    expect(await screen.findByText('Few seeders (2)')).toBeInTheDocument()
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('GET /api/auth/me')
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(screen.queryByRole('button', { name: 'Pin this show' })).not.toBeInTheDocument()
  })

  it('says so when the pin is refused', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: detail() },
      [`PUT ${PIN_PATH}`]: { status: 500, body: { detail: 'boom' } },
    })

    renderShow()
    await screen.findByText(LINE)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Pin this show' }))

    expect(await screen.findByText('Could not pin the show. Try again.')).toBeInTheDocument()
  })
})
