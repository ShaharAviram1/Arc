import { QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Search } from '@/pages/Search'
import { EMPTY_SEARCH, FRIEREN, FRIEREN_SPECIAL, SEARCH_PAGE_1 } from '@/test/animeFixtures'
import { mockApi, requestsMade } from '@/test/apiMock'

function renderSearch(path = '/search') {
  const router = createMemoryRouter(
    [
      { path: '/search', element: <Search /> },
      { path: '/anime/:id', element: <p>show page</p> },
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

describe('Search', () => {
  it('shows the idle hint before anything is typed', () => {
    mockApi({})
    renderSearch()

    expect(screen.getByText('Search the catalogue by title')).toBeInTheDocument()
  })

  it('debounces typing into a single request and mirrors the query in the URL', async () => {
    vi.useFakeTimers()
    const fetchMock = mockApi({
      'GET /api/anime/search?q=frie&page=1': { body: SEARCH_PAGE_1 },
    })

    const router = renderSearch()
    const input = screen.getByLabelText('Search anime')

    // Four keystrokes, none of them a pause long enough to fire the request.
    for (const value of ['f', 'fr', 'fri', 'frie']) {
      fireEvent.change(input, { target: { value } })
      act(() => {
        vi.advanceTimersByTime(50)
      })
    }

    // The URL follows every keystroke; the request waits for the pause.
    expect(router.state.location.search).toBe('?q=frie')
    expect(requestsMade(fetchMock)).toHaveLength(0)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300)
    })

    expect(requestsMade(fetchMock)).toEqual(['GET /api/anime/search?q=frie&page=1'])

    // Let the answer land; still exactly one request.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(screen.getByText(FRIEREN.title.preferred)).toBeInTheDocument()
    expect(requestsMade(fetchMock)).toHaveLength(1)
  })

  it('renders results for a query that arrives in the URL', async () => {
    mockApi({ 'GET /api/anime/search?q=frieren&page=1': { body: SEARCH_PAGE_1 } })

    renderSearch('/search?q=frieren')

    expect(await screen.findByText(FRIEREN.title.preferred)).toBeInTheDocument()
    expect(screen.getByText('TV · 28 eps · 2023')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: FRIEREN.title.preferred })).toHaveAttribute(
      'href',
      `/anime/${FRIEREN.id}`,
    )
    // Each card carries its own status control (spec §4.1 FR-C2).
    expect(screen.getAllByRole('combobox')).toHaveLength(2)
  })

  it('tags only the results that came from MAL (FR-C6)', async () => {
    mockApi({ 'GET /api/anime/search?q=frieren&page=1': { body: SEARCH_PAGE_1 } })

    renderSearch('/search?q=frieren')
    await screen.findByText(FRIEREN.title.preferred)

    // FRIEREN is AniList-sourced, FRIEREN_SPECIAL is not: exactly one tag.
    expect(SEARCH_PAGE_1.results.filter((anime) => anime.source === 'mal')).toEqual([
      FRIEREN_SPECIAL,
    ])
    expect(screen.getAllByText('via MAL')).toHaveLength(1)
  })

  it('names the outage when both catalogue sources are down', async () => {
    mockApi({
      'GET /api/anime/search?q=frieren&page=1': {
        status: 502,
        body: { detail: 'catalogue is unavailable' },
      },
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
    })

    renderSearch('/search?q=frieren')

    expect(await screen.findByText('Search failed. Try again.')).toBeInTheDocument()
  })

  it('shows the empty state when nothing matches', async () => {
    mockApi({ 'GET /api/anime/search?q=zzzzz&page=1': { body: EMPTY_SEARCH } })

    renderSearch('/search?q=zzzzz')

    expect(await screen.findByText('No results for “zzzzz”')).toBeInTheDocument()
  })

  it('pages forward when there is another page', async () => {
    const fetchMock = mockApi({
      'GET /api/anime/search?q=frieren&page=1': { body: SEARCH_PAGE_1 },
      'GET /api/anime/search?q=frieren&page=2': {
        body: { results: [], page: 2, has_next: false },
      },
    })

    renderSearch('/search?q=frieren')
    await screen.findByText(FRIEREN.title.preferred)
    await userEvent.setup().click(screen.getByRole('button', { name: 'More' }))

    await screen.findByText('No more results')
    expect(requestsMade(fetchMock)).toContain('GET /api/anime/search?q=frieren&page=2')
    expect(screen.getByRole('button', { name: 'Previous' })).toBeEnabled()
  })
})
