import { QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { summaryLine } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import { Review } from '@/pages/Review'
import {
  EMPTY_REVIEW_PAGE,
  FRIEREN,
  FRIEREN_SPECIAL,
  REVIEW_FILE_NAME,
  REVIEW_OTHER_NAME,
  REVIEW_PAGE,
  REVIEW_PAGE_AUTO,
  REVIEW_PAGE_IGNORED,
  REVIEW_PAGE_NO_SUGGESTIONS,
  REVIEW_PAGE_SUGGESTED,
  REVIEW_PAGE_SUGGESTED_BARE,
  REVIEW_PAGE_SUGGESTION_FAILED,
  REVIEW_PAGE_WITH_REASON,
  REVIEW_PAGE_WITHOUT_REASON,
  REVIEW_REASON_LINE,
  REVIEW_SUGGESTION_ERROR,
  REVIEW_SUGGESTION_REASON,
  SEARCH_PAGE_1,
  reviewItem,
} from '@/test/animeFixtures'
import { callTo, jsonBodyOf, mockApi, requestsMade, type MockRoutes } from '@/test/apiMock'

const PENDING = 'GET /api/review?state=pending'
const IGNORED = 'GET /api/review?state=ignored'
const AUTO = 'GET /api/review?state=auto'
const CONFIRM = 'POST /api/review/501/confirm'
const IGNORE = 'POST /api/review/501/ignore'
const REOPEN = 'POST /api/review/502/reopen'
const SUGGEST = 'POST /api/review/501/suggest'
const SEARCH = 'GET /api/review/501/search?q=frieren'

/** The item a confirm answers with: linked, and no matcher score left. */
const CONFIRMED = reviewItem({ review_state: 'confirmed', episode_id: 9005, confidence: null })

/**
 * Renders the page against a *mutable* route table, so a test can change what
 * the queue answers with between the action and the refetch it triggers —
 * which is the only way to see a card actually leave the list.
 */
function renderReview(routes: MockRoutes) {
  const fetchMock = mockApi(routes)
  const router = createMemoryRouter(
    [
      { path: '/review', element: <Review /> },
      { path: '/anime/:id', element: <p>show page</p> },
    ],
    { initialEntries: ['/review'] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return fetchMock
}

/** The one pending card, once it has arrived. */
async function card() {
  return await screen.findByRole('article', { name: REVIEW_FILE_NAME })
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Review', () => {
  it('explains the queue and lists the file with what the parser made of it', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE } })

    expect(screen.getByRole('heading', { level: 1, name: 'Match review' })).toBeInTheDocument()
    expect(screen.getByText(/Nothing from them is prepared or played/)).toBeInTheDocument()

    const article = await card()
    for (const chip of ['Sousou no Frieren', 'E05', 'SubsPlease', '1080p']) {
      expect(within(article).getByText(chip)).toBeInTheDocument()
    }
    expect(within(article).getByText('62% sure')).toBeInTheDocument()
    expect(within(article).getByText(/downloads\/Frieren/)).toBeInTheDocument()
    expect(within(article).getByText(/1\.4 GB/)).toBeInTheDocument()
    // The pending count comes off the page, not off the list length.
    expect(screen.getByRole('button', { name: 'Pending (1)' })).toBeInTheDocument()
  })

  it('renders the tabs as M15 chips, the pending one carrying its count', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE } })
    await card()

    const pending = screen.getByRole('button', { name: 'Pending (1)' })
    // A 44px pill, brighter when it is the one that is on — the chip spec.
    expect(pending).toHaveClass('h-11', 'rounded-full')
    expect(pending).toHaveAttribute('aria-pressed', 'true')

    const ignored = screen.getByRole('button', { name: 'Ignored' })
    expect(ignored).toHaveClass('h-11', 'rounded-full')
    expect(ignored).toHaveAttribute('aria-pressed', 'false')
  })

  it('shows the matcher’s candidates with their scores and reasons', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE } })
    const article = await card()

    expect(within(article).getByRole('link', { name: FRIEREN.title.preferred })).toHaveAttribute(
      'href',
      `/anime/${String(FRIEREN.id)}`,
    )
    expect(within(article).getByText('62%')).toBeInTheDocument()
    expect(within(article).getByText(/title similarity 0\.62/)).toBeInTheDocument()
    expect(
      within(article).getByRole('link', { name: FRIEREN_SPECIAL.title.preferred }),
    ).toBeInTheDocument()
    expect(within(article).getByText(/absolute numbering/)).toBeInTheDocument()
  })

  it('labels the matcher’s parting sentence rather than leaving it bare', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE_WITH_REASON } })
    const article = await card()

    // Unlabelled, "below the auto-link threshold" under a list of shows reads
    // as a comment on the last one.
    expect(within(article).getByText(`Why it is here: ${REVIEW_REASON_LINE}`)).toBeInTheDocument()
    // Still the matcher talking, not the page: it stays muted.
    expect(within(article).getByText(`Why it is here: ${REVIEW_REASON_LINE}`)).toHaveClass(
      'text-[var(--arc-text-muted)]',
    )
  })

  it('says nothing where the matcher gave no reason', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE_WITHOUT_REASON } })
    const article = await card()

    expect(within(article).queryByText(/Why it is here/)).not.toBeInTheDocument()
    // And the candidates it did offer are untouched by the empty entry.
    expect(
      within(article).getByRole('button', { name: `Choose ${FRIEREN.title.preferred}` }),
    ).toBeInTheDocument()
  })

  it('gives a candidate one text column, so a long title is not squeezed to nothing', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE } })
    const article = await card()

    const title = within(article).getByRole('link', { name: FRIEREN.title.preferred })
    const column = title.parentElement
    expect(column).toHaveClass('min-w-0', 'flex-1')

    // The summary line and the score are siblings of the title *inside* that
    // column. As flex items of the row they would each claim width of their
    // own and leave the title a few pixels to wrap in.
    expect(column).toContainElement(within(article).getByText(summaryLine(FRIEREN)))
    expect(column).toContainElement(within(article).getByText('62%'))
    expect(column).toContainElement(within(article).getByText(/title similarity 0\.62/))

    // The row is exactly cover, column, button — nothing else to compete.
    const row = column?.parentElement
    expect(row?.children).toHaveLength(3)
    expect(row?.lastElementChild).toHaveAttribute('aria-label', `Choose ${FRIEREN.title.preferred}`)

    // Breaking mid-word belongs to the filename, which has no spaces to break
    // at, and to nothing else on the card.
    expect(within(article).getByText(REVIEW_FILE_NAME)).toHaveClass('break-all')
    expect(title.className).not.toMatch(/break-all/)
    // No fixed width on the column either — `min-w-0` is the point, `w-24`
    // would be the bug in another form.
    expect(column?.className).not.toMatch(/break-all|(^|\s)w-/)
  })

  it('fills the form from a candidate and confirms it as anime plus episode', async () => {
    const routes: MockRoutes = {
      [PENDING]: { body: REVIEW_PAGE },
      [CONFIRM]: { body: CONFIRMED },
    }
    const fetchMock = renderReview(routes)
    const article = await card()

    expect(within(article).getByText(/Nothing chosen yet/)).toBeInTheDocument()
    expect(within(article).getByRole('button', { name: 'Confirm' })).toBeDisabled()

    await userEvent.click(
      within(article).getByRole('button', { name: `Choose ${FRIEREN.title.preferred}` }),
    )

    expect(within(article).getByText(/Confirming as/)).toBeInTheDocument()
    expect(within(article).getByLabelText('Episode number')).toHaveValue(5)

    // The card leaves the queue once the file is linked.
    routes[PENDING] = { body: EMPTY_REVIEW_PAGE }
    await userEvent.click(within(article).getByRole('button', { name: 'Confirm' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(CONFIRM)
    })
    expect(jsonBodyOf(callTo(fetchMock, '/api/review/501/confirm'))).toEqual({
      anime_id: FRIEREN.id,
      episode_number: 5,
    })
    expect(await screen.findByRole('status')).toHaveTextContent(/episode 5/)
    await waitFor(() => {
      expect(screen.queryByRole('article', { name: REVIEW_FILE_NAME })).not.toBeInTheDocument()
    })
  })

  it('lets the episode number be set by hand over whatever was parsed', async () => {
    const fetchMock = renderReview({
      [PENDING]: { body: REVIEW_PAGE },
      [CONFIRM]: { body: CONFIRMED },
    })
    const article = await card()

    await userEvent.click(
      within(article).getByRole('button', { name: `Choose ${FRIEREN.title.preferred}` }),
    )
    const input = within(article).getByLabelText('Episode number')
    await userEvent.clear(input)
    await userEvent.type(input, '17')
    await userEvent.click(within(article).getByRole('button', { name: 'Confirm' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(CONFIRM)
    })
    expect(jsonBodyOf(callTo(fetchMock, '/api/review/501/confirm')).episode_number).toBe(17)
  })

  it('will not confirm until both halves of the answer are there', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE } })
    const article = await card()

    await userEvent.click(
      within(article).getByRole('button', { name: `Choose ${FRIEREN.title.preferred}` }),
    )
    const input = within(article).getByLabelText('Episode number')
    await userEvent.clear(input)

    expect(within(article).getByRole('button', { name: 'Confirm' })).toBeDisabled()
  })

  it('says the file is already linked rather than pretending the click worked', async () => {
    renderReview({
      [PENDING]: { body: REVIEW_PAGE },
      [CONFIRM]: {
        status: 409,
        body: { detail: 'this file is already linked to an episode' },
      },
    })
    const article = await card()

    await userEvent.click(
      within(article).getByRole('button', { name: `Choose ${FRIEREN.title.preferred}` }),
    )
    await userEvent.click(within(article).getByRole('button', { name: 'Confirm' }))

    expect(await within(article).findByRole('alert')).toHaveTextContent(/already linked/i)
  })

  it('takes a file out of the queue when it is not anime', async () => {
    const routes: MockRoutes = {
      [PENDING]: { body: REVIEW_PAGE },
      [IGNORE]: { body: reviewItem({ review_state: 'ignored' }) },
    }
    const fetchMock = renderReview(routes)
    const article = await card()

    routes[PENDING] = { body: EMPTY_REVIEW_PAGE }
    await userEvent.click(within(article).getByRole('button', { name: 'Ignore (not anime)' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(IGNORE)
    })
    await waitFor(() => {
      expect(screen.queryByRole('article', { name: REVIEW_FILE_NAME })).not.toBeInTheDocument()
    })
    expect(screen.getByRole('status')).toHaveTextContent(/marked as not anime/)
  })

  it('shows a suggestion as a suggestion, named and never applied', async () => {
    const fetchMock = renderReview({ [PENDING]: { body: REVIEW_PAGE_SUGGESTED } })
    const article = await card()

    expect(within(article).getByRole('heading', { name: 'Suggestion' })).toBeInTheDocument()
    expect(
      within(article).getByText('from claude-opus-5, never applied automatically'),
    ).toBeInTheDocument()
    expect(within(article).getByText(REVIEW_SUGGESTION_REASON)).toBeInTheDocument()
    expect(within(article).getByText('high confidence')).toBeInTheDocument()
    expect(within(article).getByText('Episode 5')).toBeInTheDocument()

    // "Use this" fills the form. It must not confirm anything.
    await userEvent.click(within(article).getByRole('button', { name: 'Use this' }))

    expect(within(article).getByText(/Confirming as/)).toBeInTheDocument()
    expect(within(article).getByLabelText('Episode number')).toHaveValue(5)
    expect(requestsMade(fetchMock)).not.toContain(CONFIRM)
  })

  it('says why there is no suggestion when the ask came back empty', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE_SUGGESTION_FAILED } })
    const article = await card()

    expect(
      within(article).getByText(`No suggestion: ${REVIEW_SUGGESTION_ERROR}`),
    ).toBeInTheDocument()
    expect(within(article).queryByRole('button', { name: 'Use this' })).not.toBeInTheDocument()
    // A failed ask has an explanation, not a case: nothing is invented to
    // stand in for the confidence or the argument it never produced.
    expect(within(article).queryByText(/confidence/)).not.toBeInTheDocument()
  })

  it('invents neither a confidence nor an argument the model did not give', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE_SUGGESTED_BARE } })
    const article = await card()

    // The proposal itself still stands, and is still usable.
    expect(within(article).getByRole('heading', { name: 'Suggestion' })).toBeInTheDocument()
    expect(within(article).getByRole('button', { name: 'Use this' })).toBeInTheDocument()
    expect(within(article).getByText('Episode 5')).toBeInTheDocument()

    expect(within(article).queryByText(/confidence/)).not.toBeInTheDocument()
    expect(within(article).queryByText(REVIEW_SUGGESTION_REASON)).not.toBeInTheDocument()
  })

  it('asks for a suggestion and then says it is waiting for one', async () => {
    const fetchMock = renderReview({
      [PENDING]: { body: REVIEW_PAGE },
      [SUGGEST]: { status: 202, body: { job_id: 42, status: 'pending' } },
    })
    const article = await card()

    await userEvent.click(within(article).getByRole('button', { name: 'Ask for a suggestion' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(SUGGEST)
    })
    expect(await within(article).findByText('Requested — refresh in a moment.')).toBeInTheDocument()
    expect(
      within(article).queryByRole('button', { name: 'Ask for a suggestion' }),
    ).not.toBeInTheDocument()
  })

  it('offers nothing to ask when the server has suggestions switched off', async () => {
    renderReview({ [PENDING]: { body: REVIEW_PAGE_NO_SUGGESTIONS } })
    const article = await card()

    expect(
      within(article).queryByRole('button', { name: 'Ask for a suggestion' }),
    ).not.toBeInTheDocument()
  })

  it('polls the queue until the requested suggestion lands, then stops', async () => {
    vi.useFakeTimers()
    const routes: MockRoutes = {
      [PENDING]: { body: REVIEW_PAGE },
      [SUGGEST]: { status: 202, body: { job_id: 42, status: 'pending' } },
    }
    const fetchMock = mockApi(routes)
    const router = createMemoryRouter([{ path: '/review', element: <Review /> }], {
      initialEntries: ['/review'],
    })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
    fireEvent.click(screen.getByRole('button', { name: 'Ask for a suggestion' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })

    // The job answers a moment later; the poll is what notices.
    routes[PENDING] = { body: REVIEW_PAGE_SUGGESTED }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })

    expect(screen.getByRole('heading', { name: 'Suggestion' })).toBeInTheDocument()

    // Having arrived, it stops costing a request every ten seconds.
    const settled = requestsMade(fetchMock).length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(requestsMade(fetchMock)).toHaveLength(settled)
  })

  it('searches for a title the matcher never proposed and lets it be chosen', async () => {
    vi.useFakeTimers()
    const routes: MockRoutes = {
      [PENDING]: { body: REVIEW_PAGE },
      [SEARCH]: { body: SEARCH_PAGE_1 },
      [CONFIRM]: { body: CONFIRMED },
    }
    const fetchMock = mockApi(routes)
    const router = createMemoryRouter([{ path: '/review', element: <Review /> }], {
      initialEntries: ['/review'],
    })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })

    const input = screen.getByLabelText('Search another title')
    for (const value of ['fri', 'frie', 'frier', 'frieren']) {
      fireEvent.change(input, { target: { value } })
      act(() => {
        vi.advanceTimersByTime(50)
      })
    }
    // Typing costs nothing until it stops.
    expect(requestsMade(fetchMock)).not.toContain(SEARCH)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
    })
    expect(requestsMade(fetchMock).filter((made) => made === SEARCH)).toHaveLength(1)
    // Let the answer land and the list render.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50)
    })

    fireEvent.click(
      screen.getByRole('button', {
        name: `Choose ${FRIEREN_SPECIAL.title.preferred} from search results`,
      }),
    )
    expect(screen.getByText(/Confirming as/)).toBeInTheDocument()

    // A search result carries no episode number, so the parsed one stands.
    expect(screen.getByLabelText('Episode number')).toHaveValue(5)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })

    expect(jsonBodyOf(callTo(fetchMock, '/api/review/501/confirm'))).toEqual({
      anime_id: FRIEREN_SPECIAL.id,
      episode_number: 5,
    })
  })

  it('switches the state the queue is listed in', async () => {
    const fetchMock = renderReview({
      [PENDING]: { body: REVIEW_PAGE },
      [IGNORED]: { body: REVIEW_PAGE_IGNORED },
      [AUTO]: { body: REVIEW_PAGE_AUTO },
    })
    await card()

    await userEvent.click(screen.getByRole('button', { name: 'Ignored' }))
    expect(await screen.findByRole('article', { name: REVIEW_OTHER_NAME })).toBeInTheDocument()
    expect(requestsMade(fetchMock)).toContain(IGNORED)

    await userEvent.click(screen.getByRole('button', { name: 'Auto-linked' }))
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(AUTO)
    })
    const row = await screen.findByRole('listitem', {
      name: '[SubsPlease] Sousou no Frieren - 04 (1080p) [A1B2C3D4].mkv',
    })
    expect(within(row).getByRole('link', { name: FRIEREN.title.preferred })).toBeInTheDocument()
    expect(within(row).getByText(/episode 4/)).toBeInTheDocument()
    // Read-only: nothing on this tab acts on anything.
    expect(within(row).queryByRole('button')).not.toBeInTheDocument()
  })

  it('puts an ignored file back in the queue', async () => {
    const routes: MockRoutes = {
      [IGNORED]: { body: REVIEW_PAGE_IGNORED },
      [PENDING]: { body: REVIEW_PAGE },
      [REOPEN]: { body: reviewItem({ id: 502, review_state: 'pending' }) },
    }
    const fetchMock = renderReview(routes)

    await userEvent.click(screen.getByRole('button', { name: 'Ignored' }))
    const article = await screen.findByRole('article', { name: REVIEW_OTHER_NAME })

    routes[IGNORED] = { body: { items: [], pending: 2, suggestions_enabled: true } }
    await userEvent.click(within(article).getByRole('button', { name: 'Reopen' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(REOPEN)
    })
    expect(await screen.findByRole('status')).toHaveTextContent(/back in the queue/)
  })

  it('has something to say on every tab when there is nothing on it', async () => {
    const empty = { items: [], pending: 0, suggestions_enabled: true }
    renderReview({
      [PENDING]: { body: empty },
      [IGNORED]: { body: empty },
      [AUTO]: { body: empty },
    })

    expect(await screen.findByText(/Nothing waiting/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Ignored' }))
    expect(await screen.findByText(/Nothing ignored/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Auto-linked' }))
    expect(await screen.findByText(/Nothing auto-linked/)).toBeInTheDocument()
  })

  it('offers a way back when the queue itself will not load', async () => {
    const routes: MockRoutes = { [PENDING]: { status: 500, body: { detail: 'boom' } } }
    const fetchMock = renderReview(routes)

    expect(await screen.findByRole('alert')).toHaveTextContent(/Something went wrong/)

    routes[PENDING] = { body: REVIEW_PAGE }
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('article', { name: REVIEW_FILE_NAME })).toBeInTheDocument()
    expect(requestsMade(fetchMock).filter((made) => made === PENDING).length).toBeGreaterThan(1)
  })
})
