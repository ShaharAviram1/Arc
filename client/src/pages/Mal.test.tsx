import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Mal } from '@/pages/Mal'
import {
  FRIEREN,
  MAL_LOG,
  MAL_SKIP_REASON,
  MAL_STATUS_LINKED,
  MAL_STATUS_NEEDS_RELINK,
  MAL_STATUS_UNCONFIGURED,
  MAL_STATUS_UNLINKED,
  MAL_WRITE_ERROR,
  malWrite,
} from '@/test/animeFixtures'
import { mockApi, requestsMade, type MockRoutes } from '@/test/apiMock'
import { stubLocationAssign } from '@/test/location'

const STATUS_PATH = 'GET /api/mal/status'
const LOG_PATH = 'GET /api/mal/log?limit=50'
const AUTHORIZE_URL = 'https://myanimelist.net/v1/oauth2/authorize?client_id=arc&state=abc'

function renderMal(path = '/mal') {
  const router = createMemoryRouter(
    [
      { path: '/mal', element: <Mal /> },
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

/** The linked page with an empty log, which most action tests do not care about. */
function linkedRoutes(extra: MockRoutes = {}): MockRoutes {
  return { [STATUS_PATH]: { body: MAL_STATUS_LINKED }, [LOG_PATH]: { body: [] }, ...extra }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Mal', () => {
  it('explains an unconfigured server and offers nothing to click', async () => {
    const fetchMock = mockApi({ [STATUS_PATH]: { body: MAL_STATUS_UNCONFIGURED } })

    renderMal()

    expect(await screen.findByText(/no MyAnimeList client id set/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect MyAnimeList' })).not.toBeInTheDocument()
    // No link, no writes: the log is not even asked for.
    expect(requestsMade(fetchMock)).toEqual(['GET /api/mal/status'])
  })

  it('reports a status the server could not give, with a way to ask again', async () => {
    const fetchMock = mockApi({ [STATUS_PATH]: { status: 500, body: { detail: 'boom' } } })

    renderMal()

    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong. Try again.')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === 'GET /api/mal/status')).toHaveLength(
        2,
      )
    })
  })

  describe('not linked', () => {
    it('says what Arc will read and write before anything is authorised', async () => {
      mockApi({ [STATUS_PATH]: { body: MAL_STATUS_UNLINKED }, [LOG_PATH]: { body: [] } })

      renderMal()

      expect(await screen.findByRole('button', { name: 'Connect MyAnimeList' })).toBeInTheDocument()
      expect(
        screen.getByText(/reads your MyAnimeList list once as the baseline/i),
      ).toBeInTheDocument()
      expect(screen.getByText(/writes back only changes you make in Arc/i)).toBeInTheDocument()
      expect(await screen.findByText('Nothing written to MyAnimeList yet.')).toBeInTheDocument()
    })

    it('posts for an authorize URL and sends the browser there', async () => {
      const fetchMock = mockApi({
        [STATUS_PATH]: { body: MAL_STATUS_UNLINKED },
        [LOG_PATH]: { body: [] },
        'POST /api/mal/link': { body: { authorize_url: AUTHORIZE_URL } },
      })
      const assign = stubLocationAssign()

      renderMal()
      await userEvent.click(await screen.findByRole('button', { name: 'Connect MyAnimeList' }))

      await waitFor(() => {
        expect(assign).toHaveBeenCalledWith(AUTHORIZE_URL)
      })
      expect(requestsMade(fetchMock)).toContain('POST /api/mal/link')
    })

    it('says why the link could not be started', async () => {
      mockApi({
        [STATUS_PATH]: { body: MAL_STATUS_UNLINKED },
        [LOG_PATH]: { body: [] },
        'POST /api/mal/link': { status: 409, body: { detail: 'already linked' } },
      })
      stubLocationAssign()

      renderMal()
      await userEvent.click(await screen.findByRole('button', { name: 'Connect MyAnimeList' }))

      expect(await screen.findByRole('alert')).toHaveTextContent('already linked')
    })
  })

  describe('linked', () => {
    it('names the account and offers import, push and disconnect', async () => {
      mockApi(linkedRoutes())

      renderMal()

      expect(await screen.findByText('arcviewer')).toBeInTheDocument()
      expect(screen.getByText(/Last import:/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Import now' })).toBeInTheDocument()
      // Fields, not shows: the label says "writes" so the count cannot be
      // read as "two shows".
      expect(screen.getByRole('button', { name: 'Push 2 pending writes' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument()
      expect(screen.getByText('1 write failed and was not applied.')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Reconnect' })).not.toBeInTheDocument()
    })

    it('counts a single queued field in the singular', async () => {
      mockApi({
        [STATUS_PATH]: { body: { ...MAL_STATUS_LINKED, pending_writes: 1 } },
        [LOG_PATH]: { body: [] },
      })

      renderMal()

      expect(
        await screen.findByRole('button', { name: 'Push 1 pending write' }),
      ).toBeInTheDocument()
    })

    it('hides "push pending" when nothing is queued', async () => {
      mockApi({
        [STATUS_PATH]: { body: { ...MAL_STATUS_LINKED, pending_writes: 0, failed_writes: 0 } },
        [LOG_PATH]: { body: [] },
      })

      renderMal()

      expect(await screen.findByRole('button', { name: 'Import now' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /pending write/ })).not.toBeInTheDocument()
    })

    it('asks for an import', async () => {
      const fetchMock = mockApi(linkedRoutes({ 'POST /api/mal/import': { status: 202 } }))

      renderMal()
      await userEvent.click(await screen.findByRole('button', { name: 'Import now' }))

      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain('POST /api/mal/import')
      })
    })

    it('asks for a push of the queued writes', async () => {
      const fetchMock = mockApi(linkedRoutes({ 'POST /api/mal/push': { status: 202 } }))

      renderMal()
      await userEvent.click(await screen.findByRole('button', { name: 'Push 2 pending writes' }))

      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain('POST /api/mal/push')
      })
    })

    it('warns and offers a reconnect when the token stopped working', async () => {
      mockApi({
        [STATUS_PATH]: { body: MAL_STATUS_NEEDS_RELINK },
        [LOG_PATH]: { body: [] },
        'POST /api/mal/link': { body: { authorize_url: AUTHORIZE_URL } },
      })
      const assign = stubLocationAssign()

      renderMal()

      const banner = await screen.findByRole('alert')
      expect(banner).toHaveTextContent(/stopped accepting/i)
      await userEvent.click(within(banner).getByRole('button', { name: 'Reconnect' }))

      await waitFor(() => {
        expect(assign).toHaveBeenCalledWith(AUTHORIZE_URL)
      })
    })

    it('confirms a disconnect inline before sending it', async () => {
      const fetchMock = mockApi(linkedRoutes({ 'DELETE /api/mal/link': { status: 204 } }))

      renderMal()
      await userEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))

      expect(screen.getByText('Really disconnect?')).toBeInTheDocument()
      expect(requestsMade(fetchMock)).not.toContain('DELETE /api/mal/link')

      // "No" backs out without a request.
      await userEvent.click(screen.getByRole('button', { name: 'No' }))
      expect(screen.queryByText('Really disconnect?')).not.toBeInTheDocument()
      expect(requestsMade(fetchMock)).not.toContain('DELETE /api/mal/link')

      await userEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
      await userEvent.click(screen.getByRole('button', { name: 'Yes' }))

      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain('DELETE /api/mal/link')
      })
    })
  })

  describe('the redirect back from MAL', () => {
    it('reports a successful link and cleans the query', async () => {
      mockApi(linkedRoutes())

      const router = renderMal('/mal?linked=1')

      expect(await screen.findByRole('status')).toHaveTextContent('MyAnimeList connected.')
      await waitFor(() => {
        expect(router.state.location.search).toBe('')
      })
      // The message survives the clean-up; only the URL is tidied.
      expect(screen.getByRole('status')).toHaveTextContent('MyAnimeList connected.')
    })

    it('turns an error code into something readable and cleans the query', async () => {
      mockApi({ [STATUS_PATH]: { body: MAL_STATUS_UNLINKED }, [LOG_PATH]: { body: [] } })

      const router = renderMal('/mal?error=access_denied')

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent(/declined the request on MyAnimeList/i)
      await waitFor(() => {
        expect(router.state.location.search).toBe('')
      })
    })

    it('keeps other query parameters', async () => {
      mockApi(linkedRoutes())

      const router = renderMal('/mal?linked=1&keep=me')

      await waitFor(() => {
        expect(router.state.location.search).toBe('?keep=me')
      })
    })
  })

  describe('the write log', () => {
    it('renders each write with its formatted values, cause and status', async () => {
      mockApi({ [STATUS_PATH]: { body: MAL_STATUS_LINKED }, [LOG_PATH]: { body: MAL_LOG } })

      renderMal()

      // A progress write that landed; `ok` reads as "Synced", not "Done".
      expect(await screen.findByText('3 → 4')).toBeInTheDocument()
      // Two rows are watch-caused: the one that landed and the skipped one.
      expect(screen.getAllByText('Watched')).toHaveLength(2)
      expect(screen.getByText('Synced')).toBeInTheDocument()
      expect(screen.queryByText('Done')).not.toBeInTheDocument()
      // A status write that failed: labels, not wire enums, and MAL's reason
      // both inline and on hover.
      expect(screen.getByText('Watching → On hold')).toBeInTheDocument()
      expect(screen.getByText(MAL_WRITE_ERROR)).toBeInTheDocument()
      expect(screen.getByText('Failed')).toHaveAttribute('title', MAL_WRITE_ERROR)
      // A queued score write with no previous value.
      expect(screen.getByText('— → 8')).toBeInTheDocument()
      expect(screen.getByText('Pending')).toBeInTheDocument()

      expect(screen.getAllByRole('link', { name: FRIEREN.title.preferred })).toHaveLength(2)
    })

    it('explains a skipped row instead of reading it as a failure', async () => {
      mockApi({ [STATUS_PATH]: { body: MAL_STATUS_LINKED }, [LOG_PATH]: { body: MAL_LOG } })

      renderMal()

      expect(await screen.findByText('Skipped')).toBeInTheDocument()
      // The sentence is shown inline, muted rather than in the error colour,
      // because nothing went wrong: the FR-M4 rule declined to send it.
      const reason = screen.getByText(MAL_SKIP_REASON)
      expect(reason).toBeInTheDocument()
      expect(reason.className).toContain('--arc-text-muted')
      expect(reason.className).not.toContain('--arc-error')
      expect(screen.getByText('Skipped')).toHaveAttribute('title', MAL_SKIP_REASON)
      // Nothing was written, so there is nothing to put back.
      expect(screen.getByText('9 → 2')).toBeInTheDocument()
    })

    it('offers Revert only for a revertible row, and posts it', async () => {
      const fetchMock = mockApi({
        [STATUS_PATH]: { body: MAL_STATUS_LINKED },
        [LOG_PATH]: { body: MAL_LOG },
        'POST /api/mal/log/1/revert': { status: 202 },
      })

      renderMal()

      // Two of the four fixture rows are revertible; the pending and the
      // skipped ones are not — neither has a value on MAL to put back.
      const buttons = await screen.findAllByRole('button', { name: 'Revert' })
      expect(buttons).toHaveLength(2)

      await userEvent.click(buttons[0] as HTMLElement)
      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain('POST /api/mal/log/1/revert')
      })
    })

    it('says why a revert was refused', async () => {
      mockApi({
        [STATUS_PATH]: { body: MAL_STATUS_LINKED },
        [LOG_PATH]: { body: [malWrite()] },
        'POST /api/mal/log/1/revert': { status: 409, body: { detail: 'already reverted' } },
      })

      renderMal()
      await userEvent.click(await screen.findByRole('button', { name: 'Revert' }))

      expect(await screen.findByRole('alert')).toHaveTextContent('already reverted')
    })

    it('re-asks with a status filter when the select changes', async () => {
      const fetchMock = mockApi({
        [STATUS_PATH]: { body: MAL_STATUS_LINKED },
        [LOG_PATH]: { body: MAL_LOG },
        'GET /api/mal/log?limit=50&status=failed': { body: [MAL_LOG[1]] },
      })

      renderMal()
      await screen.findByText('3 → 4')

      await userEvent.selectOptions(screen.getByLabelText('Filter writes'), 'failed')

      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain('GET /api/mal/log?limit=50&status=failed')
      })
      await waitFor(() => {
        expect(screen.queryByText('3 → 4')).not.toBeInTheDocument()
      })
      expect(screen.getByText('Watching → On hold')).toBeInTheDocument()
    })

    it('shows an empty state when the filter matches nothing', async () => {
      mockApi({
        [STATUS_PATH]: { body: MAL_STATUS_LINKED },
        [LOG_PATH]: { body: MAL_LOG },
        'GET /api/mal/log?limit=50&status=pending': { body: [] },
      })

      renderMal()
      await screen.findByText('3 → 4')
      await userEvent.selectOptions(screen.getByLabelText('Filter writes'), 'pending')

      expect(await screen.findByText('Nothing written to MyAnimeList yet.')).toBeInTheDocument()
    })
  })
})
