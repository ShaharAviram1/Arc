import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import { mockApi, requestsMade, TEST_ADMIN, TEST_USER, type MockResponse } from '@/test/apiMock'

const HEALTH = { body: { status: 'ok', version: '0.1.0', env: 'test' } }
const REVIEW_SUMMARY = 'GET /api/review/summary'
const ACQUISITION_STATUS = 'GET /api/acquisition/status'
const PAUSED_NOTE = 'Acquisition paused'

/** An acquisition status body with the flag set however we say. */
function status(paused: boolean) {
  return { body: { paused, active_wants: 4, searching: 1, downloading: 0 } }
}

/** The signed-in app at `/`, as `me`, with the acquisition status answering. */
function renderAs(me: typeof TEST_USER, acquisition?: MockResponse) {
  const fetchMock = mockApi({
    'GET /api/auth/me': { body: me },
    'GET /api/health': HEALTH,
    [REVIEW_SUMMARY]: { body: { pending: 0 } },
    ...(acquisition ? { [ACQUISITION_STATUS]: acquisition } : {}),
  })
  const router = createMemoryRouter(routes, { initialEntries: ['/'] })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return fetchMock
}

/** The signed-in app at `/`, with the review count answering however we say. */
function renderWithReview(summary: MockResponse) {
  const fetchMock = mockApi({
    'GET /api/auth/me': { body: TEST_USER },
    'GET /api/health': HEALTH,
    [REVIEW_SUMMARY]: summary,
  })
  const router = createMemoryRouter(routes, { initialEntries: ['/'] })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Layout review badge', () => {
  it('shows a count pill on the Review entry when files are waiting', async () => {
    renderWithReview({ body: { pending: 3 } })

    const pill = await screen.findByLabelText('3 files need review')
    expect(pill).toHaveTextContent('3')
    // It hangs off the nav entry rather than floating somewhere on its own.
    expect(screen.getByRole('link', { name: /Review/ })).toContainElement(pill)
  })

  it('says it in the singular for a single file', async () => {
    renderWithReview({ body: { pending: 1 } })

    expect(await screen.findByLabelText('1 file needs review')).toHaveTextContent('1')
  })

  it('shows no pill when nothing is waiting', async () => {
    const fetchMock = renderWithReview({ body: { pending: 0 } })

    await screen.findByRole('link', { name: 'Review' })
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(REVIEW_SUMMARY)
    })
    expect(screen.queryByLabelText(/need(s)? review/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Review' })).toHaveTextContent(/^Review$/)
  })

  it('shows no pill when the count cannot be fetched', async () => {
    const fetchMock = renderWithReview({ status: 500, body: { detail: 'boom' } })

    await screen.findByRole('link', { name: 'Review' })
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(REVIEW_SUMMARY)
    })
    expect(screen.queryByLabelText(/need(s)? review/)).not.toBeInTheDocument()
    // `retry: false`: a badge is not worth hammering a failing endpoint.
    expect(requestsMade(fetchMock).filter((call) => call === REVIEW_SUMMARY)).toHaveLength(1)
  })

  it('does not ask for the count while logged out', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } },
    })
    const router = createMemoryRouter(routes, { initialEntries: ['/'] })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(requestsMade(fetchMock)).not.toContain(REVIEW_SUMMARY)
  })

  it('asks for the count once per mount', async () => {
    const fetchMock = renderWithReview({ body: { pending: 2 } })

    await screen.findByLabelText('2 files need review')
    expect(requestsMade(fetchMock).filter((call) => call === REVIEW_SUMMARY)).toHaveLength(1)
  })
})

describe('Layout acquisition pause note', () => {
  it('says so in the sidebar while acquisition is paused', async () => {
    renderAs(TEST_ADMIN, status(true))

    const note = await screen.findByText(PAUSED_NOTE)
    // Announced when it appears mid-session, not only on a reload.
    expect(note).toHaveAttribute('role', 'status')
    expect(screen.getByRole('complementary')).toContainElement(note)
  })

  it('says nothing while acquisition is running', async () => {
    const fetchMock = renderAs(TEST_ADMIN, status(false))

    await screen.findByRole('link', { name: 'Home' })
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(ACQUISITION_STATUS)
    })
    expect(screen.queryByText(PAUSED_NOTE)).not.toBeInTheDocument()
  })

  it('does not ask a non-admin, and never shows them the note', async () => {
    const fetchMock = renderAs(TEST_USER, status(true))

    await screen.findByRole('link', { name: 'Home' })
    expect(requestsMade(fetchMock)).not.toContain(ACQUISITION_STATUS)
    expect(screen.queryByText(PAUSED_NOTE)).not.toBeInTheDocument()
  })

  it('says nothing when the status cannot be fetched', async () => {
    const fetchMock = renderAs(TEST_ADMIN, { status: 500, body: { detail: 'boom' } })

    await screen.findByRole('link', { name: 'Home' })
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(ACQUISITION_STATUS)
    })
    expect(screen.queryByText(PAUSED_NOTE)).not.toBeInTheDocument()
    // `retry: false`: a status line is not worth hammering a failing endpoint.
    expect(requestsMade(fetchMock).filter((call) => call === ACQUISITION_STATUS)).toHaveLength(1)
  })
})
