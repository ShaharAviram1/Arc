import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import { mockApi, TEST_USER } from '@/test/apiMock'

function renderAt(path: string) {
  const client = createQueryClient()
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

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
})
