import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import type { PackPick } from '@/lib/review'
import { Review } from '@/pages/Review'
import { EMPTY_REVIEW_PAGE, FRIEREN } from '@/test/animeFixtures'
import {
  callTo,
  jsonBodyOf,
  mockApi,
  requestsMade,
  TEST_ADMIN,
  TEST_USER,
  type MockRoutes,
} from '@/test/apiMock'

const ME = 'GET /api/auth/me'
const PENDING = 'GET /api/review?state=pending'
const PACKS = 'GET /api/review/packs'
const PICK = 'POST /api/review/packs/41/pick'
const DISMISS = 'POST /api/review/packs/41/dismiss'

const PACK: PackPick = {
  id: 41,
  anime: FRIEREN,
  episode: { id: 9007, number: 7, title: 'Like a Fairy Tale' },
  reason: '2 files claim episode 7: a - 07.mkv, a - 07v2.mkv',
  title: '[Erai-raws] Sousou no Frieren - 01 ~ 28 [1080p][BATCH]',
  candidates: [
    { file_index: 0, path: 'Frieren/a - 06.mkv', size: 1_400_000_000, parsed_number: 6 },
    { file_index: 1, path: 'Frieren/a - 07.mkv', size: 1_400_000_000, parsed_number: 7 },
    { file_index: 2, path: 'Frieren/a - 07v2.mkv', size: 1_400_000_000, parsed_number: 7 },
    { file_index: 3, path: 'Frieren/extra.mkv', size: 30_000_000, parsed_number: null },
  ],
  created_at: '2026-11-03T10:00:00Z',
}

function renderReview(routes: MockRoutes, entry = '/review') {
  const fetchMock = mockApi(routes)
  const router = createMemoryRouter(
    [
      { path: '/review', element: <Review /> },
      { path: '/anime/:id', element: <p>show page</p> },
    ],
    { initialEntries: [entry] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return fetchMock
}

const scrolled = vi.fn()

beforeEach(() => {
  scrolled.mockClear()
  Element.prototype.scrollIntoView = scrolled
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Review — season packs (M21)', () => {
  it('lists the pack with each video file, the parser reading and the pick button', async () => {
    renderReview({
      [ME]: { body: TEST_ADMIN },
      [PENDING]: { body: EMPTY_REVIEW_PAGE },
      [PACKS]: { body: [PACK] },
    })

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Season packs (1)' }),
    ).toBeInTheDocument()
    const card = await screen.findByRole('article', { name: /episode 7$/ })
    expect(within(card).getByText(PACK.title)).toBeInTheDocument()
    expect(within(card).getByText(/2 files claim episode 7/)).toBeInTheDocument()
    const files = within(card).getByRole('list', { name: 'Files in the pack' })
    expect(within(files).getAllByRole('listitem')).toHaveLength(4)
    expect(within(files).getAllByText(/the name says episode 7/)).toHaveLength(2)
    expect(within(files).getByText(/no episode number in the name/)).toBeInTheDocument()
    expect(within(files).getAllByRole('button', { name: 'This is episode 7' })).toHaveLength(4)
    expect(within(card).getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('picks the chosen file and the card leaves', async () => {
    const routes: MockRoutes = {
      [ME]: { body: TEST_ADMIN },
      [PENDING]: { body: EMPTY_REVIEW_PAGE },
      [PACKS]: { body: [PACK] },
      [PICK]: { status: 204 },
    }
    const fetchMock = renderReview(routes)
    const card = await screen.findByRole('article', { name: /episode 7$/ })
    routes[PACKS] = { body: [] }

    const rows = within(card).getAllByRole('listitem')
    const v2 = rows[2]
    expect(v2).toBeDefined()
    if (v2 === undefined) return
    await userEvent.click(within(v2).getByRole('button', { name: 'This is episode 7' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(PICK)
    })
    expect(jsonBodyOf(callTo(fetchMock, '/api/review/packs/41/pick'))).toEqual({ file_index: 2 })
    expect(await screen.findByText(/Fetching a - 07v2\.mkv as episode 7/)).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: /Season packs/ })).not.toBeInTheDocument()
    })
  })

  it("dismisses the pack, and shows the server's sentence when a pick is refused", async () => {
    const routes: MockRoutes = {
      [ME]: { body: TEST_ADMIN },
      [PENDING]: { body: EMPTY_REVIEW_PAGE },
      [PACKS]: { body: [PACK] },
      [PICK]: {
        status: 409,
        body: {
          detail: {
            code: 'not_wanted',
            message: 'Nobody wants this episode any more, so nothing was fetched.',
          },
        },
      },
      [DISMISS]: { status: 204 },
    }
    const fetchMock = renderReview(routes)
    const card = await screen.findByRole('article', { name: /episode 7$/ })

    const first = within(card).getAllByRole('button', { name: 'This is episode 7' })[0]
    expect(first).toBeDefined()
    if (first === undefined) return
    await userEvent.click(first)
    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'Nobody wants this episode any more, so nothing was fetched.',
    )

    routes[PACKS] = { body: [] }
    await userEvent.click(within(card).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(DISMISS)
    })
    expect(await screen.findByText(/Dismissed the pack/)).toBeInTheDocument()
  })

  it('scrolls to the section when opened from a show or the player', async () => {
    renderReview(
      {
        [ME]: { body: TEST_ADMIN },
        [PENDING]: { body: EMPTY_REVIEW_PAGE },
        [PACKS]: { body: [PACK] },
      },
      '/review#season-packs',
    )

    const heading = await screen.findByRole('heading', { level: 2, name: 'Season packs (1)' })
    expect(heading.closest('section')).toHaveAttribute('id', 'season-packs')
    await waitFor(() => {
      expect(scrolled).toHaveBeenCalled()
    })
  })

  it('asks nothing about packs for a viewer who is not an admin', async () => {
    const fetchMock = renderReview({
      [ME]: { body: TEST_USER },
      [PENDING]: { body: EMPTY_REVIEW_PAGE },
    })

    await screen.findByRole('heading', { level: 1, name: 'Match review' })
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(ME)
    })
    expect(requestsMade(fetchMock)).not.toContain(PACKS)
    expect(screen.queryByRole('heading', { name: /Season packs/ })).not.toBeInTheDocument()
  })
})
