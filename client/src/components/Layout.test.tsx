import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import { mockApi, requestsMade, TEST_USER, type MockResponse } from '@/test/apiMock'

const HEALTH = { body: { status: 'ok', version: '0.1.0', env: 'test' } }
const REVIEW_SUMMARY = 'GET /api/review/summary'

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
