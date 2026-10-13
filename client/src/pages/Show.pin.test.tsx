import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnimeDetail } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import { Show } from '@/pages/Show'
import { FRIEREN, FRIEREN_DETAIL } from '@/test/animeFixtures'
import { mockApi, requestsMade, TEST_DEMO_USER, TEST_USER } from '@/test/apiMock'

/**
 * "Keep files" on the show page (FR-T8, M20 T8): a toggle in the hero that
 * PUTs or DELETEs the viewer's pin, and is not offered to the demo account.
 */

const DETAIL = `GET /api/anime/${String(FRIEREN.id)}`
const PIN_PATH = `/api/anime/${String(FRIEREN.id)}/pin`

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

/** The hero, by the one `<h1>` on the page. */
async function hero(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { level: 1 })
  return heading.closest('section') as HTMLElement
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Show — Keep files (FR-T8)', () => {
  it('pins the show and then reads pressed', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: { ...FRIEREN_DETAIL, pinned: false } satisfies AnimeDetail },
      [`PUT ${PIN_PATH}`]: { status: 204 },
    })

    renderShow()
    const toggle = await within(await hero()).findByRole('button', { name: 'Keep files' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    await userEvent.setup().click(toggle)

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`PUT ${PIN_PATH}`)
    })
    await waitFor(async () => {
      expect(within(await hero()).getByRole('button', { name: /Keep files/ })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
    })
  })

  it('keeps its accessible name while saving, and says busy', async () => {
    let release: () => void = () => undefined
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: { ...FRIEREN_DETAIL, pinned: false } satisfies AnimeDetail },
    })
    const fallthrough = fetchMock.getMockImplementation()
    if (fallthrough === undefined) throw new Error('no mock')
    fetchMock.mockImplementation((input: string | URL | Request, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        return new Promise<Response>((resolve) => {
          release = () => {
            resolve(new Response(null, { status: 204 }))
          }
        })
      }
      return fallthrough(input, init)
    })

    renderShow()
    await userEvent
      .setup()
      .click(await within(await hero()).findByRole('button', { name: 'Keep files' }))

    const busy = await within(await hero()).findByRole('button', { name: 'Keep files' })
    await waitFor(() => {
      expect(busy).toHaveAttribute('aria-busy', 'true')
    })
    expect(busy).toHaveTextContent('Saving…')
    release()
    await waitFor(() => {
      expect(busy).toHaveAttribute('aria-busy', 'false')
    })
  })

  it('unpins a pinned show', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: { ...FRIEREN_DETAIL, pinned: true } satisfies AnimeDetail },
      [`DELETE ${PIN_PATH}`]: { status: 204 },
    })

    renderShow()
    const toggle = await within(await hero()).findByRole('button', { name: /Keep files/ })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    await userEvent.setup().click(toggle)

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`DELETE ${PIN_PATH}`)
    })
  })

  it('says so when the change is refused', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      [DETAIL]: { body: FRIEREN_DETAIL },
      [`PUT ${PIN_PATH}`]: { status: 500, body: { detail: 'boom' } },
    })

    renderShow()
    await userEvent
      .setup()
      .click(await within(await hero()).findByRole('button', { name: 'Keep files' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not change')
  })

  it('is not offered to the demo account', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_DEMO_USER },
      [DETAIL]: { body: FRIEREN_DETAIL },
    })

    renderShow()
    const section = await hero()
    // Wait for `me` so the absence is not just "not loaded yet".
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('GET /api/auth/me')
    })
    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(within(section).queryByRole('button', { name: /Keep files/ })).not.toBeInTheDocument()
  })
})
