import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Invite } from '@/pages/Invite'
import { callTo, jsonBodyOf, mockApi, requestsMade, TEST_USER } from '@/test/apiMock'

const TOKEN = 'tok123'

function renderInvite() {
  const client = createQueryClient()
  const router = createMemoryRouter(
    [
      { path: '/invite/:token', element: <Invite /> },
      { path: '/', element: <p>Home stub</p> },
      { path: '/login', element: <p>Login stub</p> },
    ],
    { initialEntries: [`/invite/${TOKEN}`] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Invite', () => {
  it('reports an invalid or spent token', async () => {
    mockApi({ [`GET /api/invites/${TOKEN}`]: { status: 404, body: { detail: 'not found' } } })

    renderInvite()

    expect(
      await screen.findByText('This invite link is invalid or has already been used.'),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })

  it('prefills and locks the email when the invite carries one', async () => {
    mockApi({
      [`GET /api/invites/${TOKEN}`]: {
        body: { email: 'invitee@example.com', expires_at: '2026-09-12T10:00:00Z' },
      },
    })

    renderInvite()

    const email = await screen.findByLabelText('Email')
    expect(email).toHaveValue('invitee@example.com')
    expect(email).toHaveAttribute('readonly')
  })

  it('leaves the email editable when the invite has none', async () => {
    mockApi({
      [`GET /api/invites/${TOKEN}`]: { body: { email: null, expires_at: '2026-09-12T10:00:00Z' } },
    })

    renderInvite()

    const email = await screen.findByLabelText('Email')
    expect(email).toHaveValue('')
    expect(email).not.toHaveAttribute('readonly')
  })

  it('blocks submission client-side when the passwords do not match', async () => {
    const fetchMock = mockApi({
      [`GET /api/invites/${TOKEN}`]: {
        body: { email: 'invitee@example.com', expires_at: '2026-09-12T10:00:00Z' },
      },
    })

    renderInvite()
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Password'), 'longenoughpw')
    await user.type(screen.getByLabelText('Confirm password'), 'longenoughpx')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Passwords do not match.')
    expect(requestsMade(fetchMock)).not.toContain(`POST /api/invites/${TOKEN}/accept`)
  })

  it('blocks submission client-side when the password is too short', async () => {
    const fetchMock = mockApi({
      [`GET /api/invites/${TOKEN}`]: {
        body: { email: 'invitee@example.com', expires_at: '2026-09-12T10:00:00Z' },
      },
    })

    renderInvite()
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Password'), 'short')
    await user.type(screen.getByLabelText('Confirm password'), 'short')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Password must be at least 10 characters.',
    )
    expect(requestsMade(fetchMock)).not.toContain(`POST /api/invites/${TOKEN}/accept`)
  })

  it('accepts the invite with a silently detected timezone and lands home', async () => {
    const fetchMock = mockApi({
      [`GET /api/invites/${TOKEN}`]: {
        body: { email: 'invitee@example.com', expires_at: '2026-09-12T10:00:00Z' },
      },
      [`POST /api/invites/${TOKEN}/accept`]: { status: 201, body: TEST_USER },
    })

    renderInvite()
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Password'), 'longenoughpw')
    await user.type(screen.getByLabelText('Confirm password'), 'longenoughpw')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    expect(await screen.findByText('Home stub')).toBeInTheDocument()

    const payload = jsonBodyOf(callTo(fetchMock, `/api/invites/${TOKEN}/accept`))
    expect(payload.password).toBe('longenoughpw')
    // The invite fixed the address, so the client does not try to override it.
    expect(payload.email).toBeUndefined()
    expect(typeof payload.timezone).toBe('string')
  })

  it('clears the client-side complaint as soon as a password is edited', async () => {
    mockApi({
      [`GET /api/invites/${TOKEN}`]: {
        body: { email: 'invitee@example.com', expires_at: '2026-09-12T10:00:00Z' },
      },
    })

    renderInvite()
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Password'), 'longenoughpw')
    await user.type(screen.getByLabelText('Confirm password'), 'longenoughpx')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Passwords do not match.')

    await user.type(screen.getByLabelText('Confirm password'), '{Backspace}w')

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('sends the typed address when the invite fixes no email', async () => {
    const fetchMock = mockApi({
      [`GET /api/invites/${TOKEN}`]: { body: { email: null, expires_at: '2026-09-12T10:00:00Z' } },
      [`POST /api/invites/${TOKEN}/accept`]: { status: 201, body: TEST_USER },
    })

    renderInvite()
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'newcomer@example.com')
    await user.type(screen.getByLabelText('Password'), 'longenoughpw')
    await user.type(screen.getByLabelText('Confirm password'), 'longenoughpw')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    expect(await screen.findByText('Home stub')).toBeInTheDocument()

    const payload = jsonBodyOf(callTo(fetchMock, `/api/invites/${TOKEN}/accept`))
    expect(payload.email).toBe('newcomer@example.com')
    expect(payload.password).toBe('longenoughpw')
  })

  it('points an already-registered email at sign-in', async () => {
    mockApi({
      [`GET /api/invites/${TOKEN}`]: {
        body: { email: 'invitee@example.com', expires_at: '2026-09-12T10:00:00Z' },
      },
      [`POST /api/invites/${TOKEN}/accept`]: {
        status: 409,
        body: { detail: 'email already registered' },
      },
    })

    renderInvite()
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Password'), 'longenoughpw')
    await user.type(screen.getByLabelText('Confirm password'), 'longenoughpw')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('That email is already registered. Sign in instead.')
    expect(screen.getByRole('link', { name: 'Sign in instead.' })).toHaveAttribute('href', '/login')
  })
})
