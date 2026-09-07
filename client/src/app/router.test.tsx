import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import { PLAY_INFO } from '@/test/animeFixtures'
import { mockApi, TEST_USER } from '@/test/apiMock'
import { resetHls } from '@/test/hlsMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

function renderAt(path: string) {
  const client = createQueryClient()
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  resetHls()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('router', () => {
  it('renders the not-found page for an unknown path, inside the layout', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })

    renderAt('/definitely-not-a-route')

    expect(await screen.findByText('Page not found')).toBeInTheDocument()
    expect(screen.getByText('/definitely-not-a-route')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to home' })).toBeInTheDocument()
    expect(screen.getByRole('navigation')).toBeInTheDocument()
  })

  it('renders /login without the sidebar', async () => {
    mockApi({ 'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } } })

    renderAt('/login')

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('renders /invite/:token without the sidebar and without a session', async () => {
    mockApi({ 'GET /api/invites/abc': { status: 404, body: { detail: 'not found' } } })

    renderAt('/invite/abc')

    expect(
      await screen.findByText('This invite link is invalid or has already been used.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })
  it('renders the player full-bleed, still behind the session gate', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/episodes/9001/play': { body: PLAY_INFO },
    })

    renderAt('/watch/9001')

    expect(await screen.findByText('Episode 1')).toBeInTheDocument()
    // The route is a sibling of the layout, not a child, so the sidebar and
    // its links are absent; the only nav on the page is the player's own.
    expect(screen.queryByRole('link', { name: 'Schedule' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
    const navs = screen.getAllByRole('navigation')
    expect(navs).toHaveLength(1)
    expect(navs[0]).toHaveAccessibleName('Episodes')
  })

  it('sends a logged-out viewer from the player to the login page', async () => {
    mockApi({ 'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } } })

    renderAt('/watch/9001')

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })
})
