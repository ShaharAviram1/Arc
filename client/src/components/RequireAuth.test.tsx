import { QueryClientProvider, useQuery } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { RequireAuth } from '@/components/RequireAuth'
import { apiFetch } from '@/lib/api'
import { authMeQueryKey } from '@/lib/auth'
import { createQueryClient } from '@/lib/queryClient'
import { Login } from '@/pages/Login'
import { mockApi, requestsMade, TEST_ADMIN, TEST_USER } from '@/test/apiMock'

const NOT_AUTHED = { status: 401, body: { detail: 'not authenticated' } }
const HEALTH = { body: { status: 'ok', version: '0.1.0', env: 'test' } }

function renderApp(path: string, client = createQueryClient()) {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return client
}

/** A guarded page whose one request fails the way an expired session does. */
function Probe() {
  const { isError } = useQuery({
    queryKey: ['probe'],
    queryFn: () => apiFetch<{ ok: boolean }>('/api/probe'),
    retry: false,
  })
  return <p>{isError ? 'probe failed' : 'probe'}</p>
}

function renderProbe(client = createQueryClient()) {
  const router = createMemoryRouter(
    [
      { path: '/login', element: <Login /> },
      { element: <RequireAuth />, children: [{ path: '/', element: <Probe /> }] },
    ],
    { initialEntries: ['/'] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return client
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('RequireAuth', () => {
  it('sends an unauthenticated visitor to the login page', async () => {
    mockApi({ 'GET /api/auth/me': NOT_AUTHED })

    renderApp('/')

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('renders the guarded page for a signed-in user', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, 'GET /api/health': HEALTH })

    renderApp('/')

    expect(await screen.findByRole('heading', { name: 'Home' })).toBeInTheDocument()
    expect(screen.getByRole('navigation')).toBeInTheDocument()
  })

  it('shows the signed-in email and a logout button in the sidebar', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/health': HEALTH,
      'POST /api/auth/logout': { status: 204 },
    })

    renderApp('/')

    expect(await screen.findByText(TEST_USER.email)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out' }))

    expect(requestsMade(fetchMock)).toContain('POST /api/auth/logout')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('bounces to login when any other request 401s mid-session', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, 'GET /api/probe': NOT_AUTHED })

    const client = renderProbe()

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(client.getQueryData(authMeQueryKey)).toBeNull()
  })

  it('reports a transport failure instead of bouncing to login', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    renderApp('/')

    expect(await screen.findByText('Can’t reach Arc')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument()
  })
})

describe('logout', () => {
  it('drops data cached for the previous session and lands on login', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/health': HEALTH,
      'POST /api/auth/logout': { status: 204 },
    })

    const client = createQueryClient()
    client.setQueryData(['probe'], 'belongs to the previous session')
    renderApp('/', client)

    await screen.findByText(TEST_USER.email)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(client.getQueryData(['probe'])).toBeUndefined()
    expect(client.getQueryData(authMeQueryKey)).toBeNull()
  })

  it('still ends the session locally when the server fails the logout', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/health': HEALTH,
      'POST /api/auth/logout': { status: 500, body: { detail: 'boom' } },
    })

    const client = createQueryClient()
    renderApp('/', client)

    await screen.findByText(TEST_USER.email)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(client.getQueryData(authMeQueryKey)).toBeNull()
  })
})

describe('RequireAdmin', () => {
  it('blocks a non-admin from /admin without redirecting', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })

    renderApp('/admin')

    expect(await screen.findByRole('heading', { name: 'Admins only' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Admin' })).not.toBeInTheDocument()
    // The chrome is still there: they are logged in, just not allowed here.
    expect(screen.getByRole('navigation')).toBeInTheDocument()
  })

  it('hides the Admin nav entry from a non-admin', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, 'GET /api/health': HEALTH })

    renderApp('/')

    await screen.findByRole('heading', { name: 'Home' })
    expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument()
  })

  it('lets an admin through and shows the Admin nav entry', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_ADMIN } })

    renderApp('/admin')

    expect(await screen.findByRole('heading', { name: 'Admin' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Admin' })).toBeInTheDocument()
  })
})
