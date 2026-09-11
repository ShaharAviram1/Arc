import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MyListItem } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import { List } from '@/pages/List'
import { APOTHECARY, FRIEREN, listEntry } from '@/test/animeFixtures'
import { mockApi, requestsMade, type MockRoutes } from '@/test/apiMock'

/** Seven of twenty-eight watched, on a show that has finished airing. */
const WATCHING_FRIEREN: MyListItem = {
  anime: FRIEREN,
  entry: listEntry({ status: 'watching', progress: 7 }),
}

/**
 * Still broadcasting, with no total episode count and no studio: the row has
 * neither a percentage nor a credit to show, and falls back to format and year.
 */
const WATCHING_APOTHECARY: MyListItem = {
  anime: { ...APOTHECARY, episodes: null, studio: null, list_status: 'watching' },
  entry: listEntry({ anime_id: APOTHECARY.id, status: 'watching', progress: 3 }),
}

function renderList(path: string, routes: MockRoutes) {
  const fetchMock = mockApi(routes)
  const router = createMemoryRouter(
    [
      { path: '/list', element: <List /> },
      { path: '/anime/:id', element: <p>show page</p> },
    ],
    { initialEntries: [path] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { fetchMock, router }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('My List', () => {
  it('opens on Watching, which is what Arc fetches episodes for', async () => {
    const { fetchMock } = renderList('/list', {
      'GET /api/list?status=watching': { body: [WATCHING_FRIEREN] },
    })

    expect(await screen.findByRole('heading', { level: 1, name: 'My List' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Watching' })).toHaveAttribute('aria-pressed', 'true')
    expect(requestsMade(fetchMock)).toEqual(['GET /api/list?status=watching'])
  })

  it('renders a row with its season progress and airing state', async () => {
    renderList('/list', {
      'GET /api/list?status=watching': { body: [WATCHING_FRIEREN, WATCHING_APOTHECARY] },
    })

    const row = (await screen.findAllByRole('link'))[0] as HTMLElement
    expect(row).toHaveAttribute('href', `/anime/${String(FRIEREN.id)}`)

    const frieren = within(row)
    expect(frieren.getByText('Episode 7 of 28 · Madhouse')).toBeInTheDocument()
    expect(frieren.getByText('25%')).toBeInTheDocument()
    expect(frieren.getByText('Finished')).toBeInTheDocument()
    expect(
      frieren.getByRole('progressbar', { name: `Season progress for ${FRIEREN.title.preferred}` }),
    ).toHaveAttribute('aria-valuenow', '25')

    // A show with no known total has no percentage to state, and says so.
    const airing = within((await screen.findAllByRole('link'))[1] as HTMLElement)
    expect(airing.getByText('Episode 3 · TV · 2026')).toBeInTheDocument()
    expect(airing.getByText('—')).toBeInTheDocument()
    expect(airing.getByText('Airing')).toBeInTheDocument()
    expect(airing.getByRole('progressbar', { name: /Season progress/ })).not.toHaveAttribute(
      'aria-valuenow',
    )
  })

  it('syncs the chips to ?status= and asks the server for that status', async () => {
    const { fetchMock, router } = renderList('/list', {
      'GET /api/list?status=watching': { body: [WATCHING_FRIEREN] },
      'GET /api/list?status=completed': { body: [] },
    })

    await screen.findByText(FRIEREN.title.preferred)
    await userEvent.click(screen.getByRole('button', { name: 'Completed' }))

    await waitFor(() => {
      expect(router.state.location.search).toBe('?status=completed')
    })
    expect(screen.getByRole('button', { name: 'Completed' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('GET /api/list?status=completed')
    })

    // Back to the default: the bare URL is the watching list, not `?status=watching`.
    await userEvent.click(screen.getByRole('button', { name: 'Watching' }))
    await waitFor(() => {
      expect(router.state.location.search).toBe('')
    })
  })

  it('reads the filter out of the URL on arrival', async () => {
    const { fetchMock } = renderList('/list?status=planned', {
      'GET /api/list?status=planned': { body: [] },
    })

    expect(await screen.findByText(/Nothing planned/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Plan to watch' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(requestsMade(fetchMock)).toEqual(['GET /api/list?status=planned'])
  })

  it('falls back to Watching for a status the API would reject', async () => {
    const { fetchMock } = renderList('/list?status=nonsense', {
      'GET /api/list?status=watching': { body: [WATCHING_FRIEREN] },
    })

    await screen.findByText(FRIEREN.title.preferred)
    expect(requestsMade(fetchMock)).toEqual(['GET /api/list?status=watching'])
  })

  it('has an empty state of its own for each status', async () => {
    renderList('/list', { 'GET /api/list?status=watching': { body: [] } })

    expect(await screen.findByText(/Nothing on the go/)).toBeInTheDocument()
  })

  it('reports a failure with a way to try again', async () => {
    const { fetchMock } = renderList('/list', {
      'GET /api/list?status=watching': { status: 500, body: { detail: 'boom' } },
    })

    // `useMyList` takes the shared default of one retry, so allow for its backoff.
    const alert = await screen.findByRole('alert', {}, { timeout: 5000 })
    expect(alert).toHaveTextContent('Could not load your list.')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(
        requestsMade(fetchMock).filter((made) => made === 'GET /api/list?status=watching').length,
      ).toBeGreaterThan(1)
    })
  })
})
