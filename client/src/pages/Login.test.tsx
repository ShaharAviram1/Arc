import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Login } from '@/pages/Login'
import { callTo, jsonBodyOf, mockApi, requestsMade, TEST_USER } from '@/test/apiMock'

const NOT_AUTHED = { status: 401, body: { detail: 'not authenticated' } }

/** Catch-all stub that reports where the login page sent us. */
function Landed() {
  const location = useLocation()
  return <p data-testid="landed">{location.pathname + location.search + location.hash}</p>
}

/**
 * Renders the login page as `RequireAuth` leaves it: with the blocked
 * `location` object stashed in router state under `from`.
 */
function renderLoginFrom(from: Record<string, string>) {
  const client = createQueryClient()
  const router = createMemoryRouter(
    [
      { path: '/login', element: <Login /> },
      { path: '*', element: <Landed /> },
    ],
    { initialEntries: [{ pathname: '/login', state: { from } }] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

function renderLogin() {
  const client = createQueryClient()
  const router = createMemoryRouter(
    [
      { path: '/login', element: <Login /> },
      { path: '/', element: <p>Home stub</p> },
    ],
    { initialEntries: ['/login'] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

async function fillAndSubmit(email: string, password: string) {
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Email'), email)
  await user.type(screen.getByLabelText('Password'), password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Login', () => {
  it('renders the form and says registration is invite-only', async () => {
    mockApi({ 'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } } })

    renderLogin()

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).toHaveAttribute('type', 'email')
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password')
    expect(screen.getByText(/invite-only/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /sign up|register/i })).not.toBeInTheDocument()
  })

  it('shows "Wrong email or password" when the server rejects the credentials', async () => {
    mockApi({
      'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      'POST /api/auth/login': { status: 401, body: { detail: 'invalid credentials' } },
    })

    renderLogin()
    await fillAndSubmit('viewer@example.com', 'wrongpassword')

    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong email or password.')
  })

  it('reports the wait when the server rate-limits the attempt', async () => {
    mockApi({
      'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      'POST /api/auth/login': {
        status: 429,
        body: { detail: 'too many requests' },
        headers: { 'Retry-After': '30' },
      },
    })

    renderLogin()
    await fillAndSubmit('viewer@example.com', 'wrongpassword')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too many attempts, try again in 30 seconds.',
    )
  })

  it('sends the credentials and navigates home on success', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      'POST /api/auth/login': { body: TEST_USER },
    })

    renderLogin()
    await fillAndSubmit('viewer@example.com', 'hunter2hunter2')

    expect(await screen.findByText('Home stub')).toBeInTheDocument()

    expect(requestsMade(fetchMock)).toContain('POST /api/auth/login')
    const init = callTo(fetchMock, '/api/auth/login')
    expect(init?.credentials).toBe('include')
    expect(jsonBodyOf(init)).toEqual({
      email: 'viewer@example.com',
      password: 'hunter2hunter2',
    })
  })

  it('submits when Enter is pressed in the password field', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': NOT_AUTHED,
      'POST /api/auth/login': { body: TEST_USER },
    })

    renderLogin()
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'viewer@example.com')
    await user.type(screen.getByLabelText('Password'), 'hunter2hunter2{Enter}')

    expect(await screen.findByText('Home stub')).toBeInTheDocument()
    expect(requestsMade(fetchMock)).toContain('POST /api/auth/login')
  })
})

/**
 * The post-login redirect is attacker-reachable: whatever path the browser was
 * on lands in router state. Only same-origin paths on this site may be used.
 */
describe('Login redirect target', () => {
  const cases: [name: string, from: Record<string, string>, expected: string][] = [
    ['a protocol-relative URL', { pathname: '//evil.com' }, '/'],
    ['a backslash-escaped host', { pathname: '/\\evil.com' }, '/'],
    ['an absolute URL', { pathname: 'http://x' }, '/'],
    ['a javascript: URL', { pathname: 'javascript:alert(1)' }, '/'],
    ['the login page itself', { pathname: '/login' }, '/'],
    [
      'a deep link with a query and a hash',
      { pathname: '/anime/5', search: '?tab=episodes', hash: '#ep3' },
      '/anime/5?tab=episodes#ep3',
    ],
  ]

  it.each(cases)('resolves %s', async (_name, from, expected) => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })

    renderLoginFrom(from)

    expect((await screen.findByTestId('landed')).textContent).toBe(expected)
  })
})
