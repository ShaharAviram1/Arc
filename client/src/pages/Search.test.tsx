import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Search } from '@/pages/Search'
import {
  EMPTY_SCHEDULE,
  EMPTY_SEARCH,
  FRIEREN,
  FRIEREN_SPECIAL,
  SCHEDULE_PAGE,
  SEARCH_PAGE_1,
} from '@/test/animeFixtures'
import { mockApi, requestsMade } from '@/test/apiMock'

function renderSearch(path = '/search') {
  const router = createMemoryRouter(
    [
      { path: '/search', element: <Search /> },
      { path: '/anime/:id', element: <p>show page</p> },
      { path: '/recs', element: <p>recs page</p> },
    ],
    { initialEntries: [path] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Browse', () => {
  it('has no search field of its own — the toolbar owns it now', async () => {
    mockApi({ 'GET /api/schedule': { body: EMPTY_SCHEDULE } })
    renderSearch()

    expect(await screen.findByText('Search the catalogue by title')).toBeInTheDocument()
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Search anime')).not.toBeInTheDocument()
  })

  it('offers the current season when nothing has been searched for', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })
    renderSearch()

    expect(
      await screen.findByText('Fall 2026 in Arc’s catalogue. Search by title to look further.'),
    ).toBeInTheDocument()
    // Every entry in the season, days first, then what has no slot; once each.
    expect(screen.getByRole('link', { name: FRIEREN.title.preferred })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'The Apothecary Diaries' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: FRIEREN_SPECIAL.title.preferred })).toBeInTheDocument()
  })

  it('renders results for a query that arrives in the URL, and counts them', async () => {
    mockApi({
      'GET /api/anime/search?q=frieren&page=1': { body: SEARCH_PAGE_1 },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')

    expect(await screen.findByText('2 results for “frieren”')).toBeInTheDocument()
    expect(screen.getByText('TV · 28 eps · 2023')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: FRIEREN.title.preferred })).toHaveAttribute(
      'href',
      `/anime/${String(FRIEREN.id)}`,
    )
    // Each card keeps its own status control (spec §4.1 FR-C2).
    expect(screen.getAllByRole('combobox')).toHaveLength(2)
  })

  it('filters the grid by genre, and drops a chip that no longer applies', async () => {
    mockApi({
      'GET /api/anime/search?q=frieren&page=1': { body: SEARCH_PAGE_1 },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')
    await screen.findByText(FRIEREN.title.preferred)

    // One chip per genre present in the results, alphabetically, behind "All".
    const chips = screen.getByRole('group', { name: 'Filter by genre' })
    expect(
      within(chips)
        .getAllByRole('button')
        .map((chip) => chip.textContent),
    ).toEqual(['All', 'Adventure', 'Drama', 'Fantasy'])
    expect(within(chips).getByRole('button', { name: 'All' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    await userEvent.click(within(chips).getByRole('button', { name: 'Adventure' }))

    expect(screen.getByRole('button', { name: 'Adventure' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText(FRIEREN.title.preferred)).toBeInTheDocument()
    // The MAL-sourced result carries no genres, so no genre chip matches it.
    expect(screen.queryByText(FRIEREN_SPECIAL.title.preferred)).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'All' }))
    expect(screen.getByText(FRIEREN_SPECIAL.title.preferred)).toBeInTheDocument()
  })

  it('sends people to the recommendations page from the chip row', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })
    renderSearch()

    expect(await screen.findByRole('link', { name: 'Recommendations' })).toHaveAttribute(
      'href',
      '/recs',
    )
  })

  it('tags only the results that came from MAL (FR-C6)', async () => {
    mockApi({
      'GET /api/anime/search?q=frieren&page=1': { body: SEARCH_PAGE_1 },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')
    await screen.findByText(FRIEREN.title.preferred)

    // FRIEREN is AniList-sourced, FRIEREN_SPECIAL is not: exactly one tag.
    expect(SEARCH_PAGE_1.results.filter((anime) => anime.source === 'mal')).toEqual([
      FRIEREN_SPECIAL,
    ])
    expect(screen.getAllByText('via MAL')).toHaveLength(1)
    expect(screen.queryByText('via offline catalogue')).not.toBeInTheDocument()
  })

  it('tags a result the weekly offline import filled (FR-C6)', async () => {
    mockApi({
      'GET /api/anime/search?q=frieren&page=1': {
        body: {
          ...SEARCH_PAGE_1,
          results: [FRIEREN, { ...FRIEREN_SPECIAL, source: 'offline' as const }],
        },
      },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')
    await screen.findByText(FRIEREN.title.preferred)

    // The offline caveat replaces the MAL one; the AniList result still says
    // nothing, which is the normal case.
    const caveat = await screen.findByText('via offline catalogue')
    expect(caveat).toHaveAttribute(
      'title',
      'Live catalogues are unavailable; this result came from the weekly offline import',
    )
    expect(screen.queryByText('via MAL')).not.toBeInTheDocument()
  })

  it('names the outage when both catalogue sources are down', async () => {
    mockApi({
      'GET /api/anime/search?q=frieren&page=1': {
        status: 502,
        body: { detail: 'catalogue is unavailable' },
      },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')

    expect(
      await screen.findByText(
        'The catalogue is unavailable right now. Try again in a few minutes.',
      ),
    ).toBeInTheDocument()
  })

  it('keeps the generic message for a failure that is not an outage', async () => {
    mockApi({
      'GET /api/anime/search?q=frieren&page=1': { status: 500, body: { detail: 'boom' } },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')

    expect(await screen.findByText('Search failed. Try again.')).toBeInTheDocument()
  })

  it('says so when the season itself cannot be loaded', async () => {
    mockApi({ 'GET /api/schedule': { status: 500, body: { detail: 'boom' } } })

    renderSearch()

    expect(
      await screen.findByText('Could not load this season. Search by title instead.'),
    ).toBeInTheDocument()
  })

  it('offers a retry that runs the same search again', async () => {
    const path = 'GET /api/anime/search?q=frieren&page=1'
    const fetchMock = mockApi({
      [path]: { status: 500, body: { detail: 'boom' } },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')
    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((made) => made === path)).toHaveLength(2)
    })
  })

  it('shows the empty state when nothing matches', async () => {
    mockApi({
      'GET /api/anime/search?q=zzzzz&page=1': { body: EMPTY_SEARCH },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=zzzzz')

    expect(await screen.findByText('No results for “zzzzz”')).toBeInTheDocument()
  })

  it('pages forward when there is another page', async () => {
    const fetchMock = mockApi({
      'GET /api/anime/search?q=frieren&page=1': { body: SEARCH_PAGE_1 },
      'GET /api/anime/search?q=frieren&page=2': {
        body: { results: [], page: 2, has_next: false },
      },
      'GET /api/schedule': { body: EMPTY_SCHEDULE },
    })

    renderSearch('/search?q=frieren')
    await screen.findByText(FRIEREN.title.preferred)
    await userEvent.setup().click(screen.getByRole('button', { name: 'More' }))

    await screen.findByText('No more results')
    expect(requestsMade(fetchMock)).toContain('GET /api/anime/search?q=frieren&page=2')
    expect(screen.getByRole('button', { name: 'Previous' })).toBeEnabled()
  })
})
