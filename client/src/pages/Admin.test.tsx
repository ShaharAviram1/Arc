import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Admin } from '@/pages/Admin'
import {
  ACCOUNTS,
  CREATED_INVITE,
  CREATED_INVITE_URL,
  DISK,
  FAILED_JOB,
  INVITES,
  JOBS,
  JOBS_SUMMARY,
  LONG_ERROR,
  OFFLINE_CATALOGUE,
  OFFLINE_NEVER,
  OFFLINE_STALE,
  OTHER_USER,
  PENDING_JOB,
  QBIT,
  QBIT_DOWN,
  RETENTION_PREVIEW,
  RETENTION_REASON,
  SETTINGS,
  WANTS,
} from '@/test/adminFixtures'
import { jsonBodyOf, mockApi, requestsMade, TEST_ADMIN, type MockRoutes } from '@/test/apiMock'

const ME = 'GET /api/auth/me'
const REVIEW_SUMMARY = 'GET /api/review/summary'
const USERS = 'GET /api/users'
const INVITES_PATH = 'GET /api/invites'
const SETTINGS_PATH = 'GET /api/settings'
const SAVE_SETTINGS = 'PUT /api/settings'
const JOBS_PATH = 'GET /api/jobs?limit=50&offset=0'
const JOBS_SUMMARY_PATH = 'GET /api/jobs/summary'
const DISK_PATH = 'GET /api/retention/disk'
const PREVIEW_PATH = 'GET /api/retention/preview'
const OFFLINE_PATH = 'GET /api/catalogue/offline'
const STATUS_PATH = 'GET /api/acquisition/status'
const WANTS_PATH = 'GET /api/acquisition/wants'
const QBIT_PATH = 'GET /api/acquisition/qbit'

const RUNNING = { paused: false, active_wants: 2, searching: 1, downloading: 1, retained_bytes: 0 }
const PAUSED = { ...RUNNING, paused: true }

/** Everything the five tabs ask for, so a test may switch between them. */
const ALL_ROUTES: MockRoutes = {
  [ME]: { body: TEST_ADMIN },
  [REVIEW_SUMMARY]: { body: { pending: 3 } },
  [USERS]: { body: ACCOUNTS },
  [INVITES_PATH]: { body: INVITES },
  [SETTINGS_PATH]: { body: SETTINGS },
  [SAVE_SETTINGS]: { body: SETTINGS },
  [JOBS_PATH]: { body: JOBS },
  [JOBS_SUMMARY_PATH]: { body: JOBS_SUMMARY },
  [DISK_PATH]: { body: DISK },
  [PREVIEW_PATH]: { body: RETENTION_PREVIEW },
  [OFFLINE_PATH]: { body: OFFLINE_CATALOGUE },
  [STATUS_PATH]: { body: RUNNING },
  [WANTS_PATH]: { body: WANTS },
  [QBIT_PATH]: { body: QBIT },
}

function renderAdmin(routes: MockRoutes = {}, entry = '/admin') {
  const fetchMock = mockApi({ ...ALL_ROUTES, ...routes })
  const router = createMemoryRouter(
    [
      { path: '/admin', element: <Admin /> },
      { path: '/review', element: <p>review page</p> },
      { path: '/anime/:id', element: <p>show page</p> },
    ],
    { initialEntries: [entry] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { fetchMock, router }
}

/**
 * The JSON body one recorded call sent.
 *
 * `callTo` matches on the path alone, which is not enough here: several of
 * these paths are read *and* written (`/api/settings`, `/api/invites`), and the
 * first call to them is always the GET that filled the page in.
 */
function bodyOf(
  fetchMock: ReturnType<typeof mockApi>,
  method: string,
  path: string,
): Record<string, unknown> {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => (init?.method ?? 'GET').toUpperCase() === method && pathOf(input) === path,
  )
  return jsonBodyOf(call?.[1])
}

/** What `fetch` was called with, whichever of its three input forms it was. */
function pathOf(input: string | URL | Request): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.href
  return input.url
}

/** The account row for one address, once the table has arrived. */
async function accountRow(email: string): Promise<HTMLElement> {
  const table = await screen.findByRole('table', { name: 'Accounts' })
  const cell = await within(table).findByText(email)
  const row = cell.closest('tr')
  if (row === null) throw new Error(`no row for ${email}`)
  return row
}

/** The wrapper around one labelled rules field, so its own buttons scope. */
function fieldOf(label: string): HTMLElement {
  const control = screen.getByLabelText(label)
  const wrapper = control.closest('div')
  if (wrapper === null) throw new Error(`no field wrapper for ${label}`)
  return wrapper
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Admin — the page and its tabs', () => {
  it('opens on Users, names the five tabs, and points at the review queue', async () => {
    renderAdmin()

    expect(screen.getByRole('heading', { level: 1, name: 'Admin' })).toBeInTheDocument()
    for (const label of ['Users', 'Rules', 'Jobs', 'Storage', 'Acquisition']) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: 'Users' })).toHaveAttribute('aria-pressed', 'true')
    // M15: the tab bar is a chip row, 44px pills, not bordered boxes.
    for (const label of ['Users', 'Rules', 'Jobs', 'Storage', 'Acquisition']) {
      expect(screen.getByRole('button', { name: label })).toHaveClass('h-11', 'rounded-full')
    }
    expect(await screen.findByRole('heading', { name: 'Accounts' })).toBeInTheDocument()

    // FR-D4: the count and the way in, not a second copy of the queue.
    expect(await screen.findByText(/3 files are waiting in the match-review queue/)).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open the review queue' })).toHaveAttribute(
      'href',
      '/review',
    )
  })

  it('writes the chosen tab into ?tab= and renders it', async () => {
    const { router } = renderAdmin()

    await userEvent.click(screen.getByRole('button', { name: 'Jobs' }))

    expect(router.state.location.search).toBe('?tab=jobs')
    expect(await screen.findByRole('table', { name: 'Jobs' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Jobs' })).toHaveAttribute('aria-pressed', 'true')

    await userEvent.click(screen.getByRole('button', { name: 'Storage' }))
    expect(router.state.location.search).toBe('?tab=storage')
    expect(await screen.findByRole('heading', { name: 'Next sweep' })).toBeInTheDocument()
  })

  it('opens the tab named in the URL', async () => {
    renderAdmin({}, '/admin?tab=acquisition')
    expect(await screen.findByRole('heading', { name: 'Active wants' })).toBeInTheDocument()
  })

  it('falls back to the first tab when ?tab= is not one of the five', async () => {
    renderAdmin({}, '/admin?tab=not-a-tab')
    expect(await screen.findByRole('heading', { name: 'Accounts' })).toBeInTheDocument()
  })
})

describe('Admin — Users (FR-D1)', () => {
  it('lists accounts with role and status, and leaves the viewer’s own row alone', async () => {
    renderAdmin()

    const self = await accountRow('admin@example.com')
    expect(within(self).getByText('(you)')).toBeInTheDocument()
    expect(within(self).getByText(/Your own account/)).toBeInTheDocument()
    expect(within(self).queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(within(self).queryByRole('button', { name: 'Make user' })).not.toBeInTheDocument()

    const other = await accountRow('leah@example.com')
    expect(within(other).getByText('user')).toBeInTheDocument()
    expect(within(other).getByText('active')).toBeInTheDocument()
    expect(within(other).getByText('2026-08-14')).toBeInTheDocument()

    const disabled = await accountRow('sam@example.com')
    expect(within(disabled).getByText('disabled')).toBeInTheDocument()
    expect(within(disabled).getByRole('button', { name: 'Reactivate' })).toBeInTheDocument()
  })

  it('asks before deactivating, then patches is_active false', async () => {
    const { fetchMock } = renderAdmin({
      'PATCH /api/users/5': { body: { ...OTHER_USER, is_active: false } },
    })

    const row = await accountRow('leah@example.com')
    await userEvent.click(within(row).getByRole('button', { name: 'Deactivate' }))

    // Nothing has been sent yet: the question comes first.
    expect(requestsMade(fetchMock)).not.toContain('PATCH /api/users/5')
    expect(within(row).getByText('Deactivate leah@example.com?')).toBeInTheDocument()

    await userEvent.click(within(row).getByRole('button', { name: 'Yes, deactivate' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('PATCH /api/users/5')
    })
    expect(bodyOf(fetchMock, 'PATCH', '/api/users/5')).toEqual({ is_active: false })
  })

  it('lets a confirmation be cancelled without sending anything', async () => {
    const { fetchMock } = renderAdmin()

    const row = await accountRow('leah@example.com')
    await userEvent.click(within(row).getByRole('button', { name: 'Deactivate' }))
    await userEvent.click(within(row).getByRole('button', { name: 'Cancel' }))

    expect(within(row).getByRole('button', { name: 'Deactivate' })).toBeInTheDocument()
    expect(requestsMade(fetchMock)).not.toContain('PATCH /api/users/5')
  })

  it('reactivates a disabled account', async () => {
    const { fetchMock } = renderAdmin({
      'PATCH /api/users/6': { body: { ...OTHER_USER, id: 6, is_active: true } },
    })

    const row = await accountRow('sam@example.com')
    await userEvent.click(within(row).getByRole('button', { name: 'Reactivate' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('PATCH /api/users/6')
    })
    expect(bodyOf(fetchMock, 'PATCH', '/api/users/6')).toEqual({ is_active: true })
  })

  it('promotes with a role patch', async () => {
    const { fetchMock } = renderAdmin({
      'PATCH /api/users/5': { body: { ...OTHER_USER, role: 'admin' } },
    })

    const row = await accountRow('leah@example.com')
    await userEvent.click(within(row).getByRole('button', { name: 'Make admin' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('PATCH /api/users/5')
    })
    expect(bodyOf(fetchMock, 'PATCH', '/api/users/5')).toEqual({ role: 'admin' })
  })

  it('shows the server’s 409 next to the row that caused it', async () => {
    renderAdmin({
      'PATCH /api/users/5': {
        status: 409,
        body: { detail: 'at least one active admin is required' },
      },
    })

    const row = await accountRow('leah@example.com')
    await userEvent.click(within(row).getByRole('button', { name: 'Make admin' }))

    expect(await within(row).findByRole('alert')).toHaveTextContent(
      'at least one active admin is required',
    )
  })

  it('creates an invite and shows the one-time link with its warning', async () => {
    const writeText = vi.fn<(value: string) => Promise<void>>().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const { fetchMock } = renderAdmin({
      'POST /api/invites': { status: 201, body: CREATED_INVITE },
    })

    await screen.findByRole('table', { name: 'Invites' })
    await userEvent.type(screen.getByLabelText('Email (optional)'), 'newcomer@example.com')
    fireEvent.change(screen.getByLabelText('Expires in (hours)'), { target: { value: '48' } })
    await userEvent.click(screen.getByRole('button', { name: 'Create invite' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/invites')
    })
    expect(bodyOf(fetchMock, 'POST', '/api/invites')).toEqual({
      email: 'newcomer@example.com',
      expires_in_hours: 48,
    })

    expect(await screen.findByText(CREATED_INVITE_URL)).toBeInTheDocument()
    expect(screen.getByText(/shown once/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    expect(writeText).toHaveBeenCalledWith(CREATED_INVITE_URL)
    expect(await screen.findByText('Copied to the clipboard.')).toBeInTheDocument()
  })

  it('sends a null email when the address is left blank', async () => {
    const { fetchMock } = renderAdmin({
      'POST /api/invites': { status: 201, body: CREATED_INVITE },
    })

    await screen.findByRole('table', { name: 'Invites' })
    await userEvent.click(screen.getByRole('button', { name: 'Create invite' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/invites')
    })
    expect(bodyOf(fetchMock, 'POST', '/api/invites')).toEqual({
      email: null,
      expires_in_hours: 168,
    })
  })

  it('revokes a pending invite and offers nothing for a used one', async () => {
    const { fetchMock } = renderAdmin({ 'DELETE /api/invites/11': { status: 204 } })

    const table = await screen.findByRole('table', { name: 'Invites' })
    const rows = within(table).getAllByRole('row')
    // Header, then the pending invite, then the used one.
    const pending = rows[1] as HTMLElement
    const used = rows[2] as HTMLElement
    expect(within(used).queryByRole('button', { name: 'Revoke' })).not.toBeInTheDocument()

    await userEvent.click(within(pending).getByRole('button', { name: 'Revoke' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('DELETE /api/invites/11')
    })
  })

  it('offers a retry when the accounts fail to load', async () => {
    const { fetchMock } = renderAdmin({ [USERS]: { status: 500, body: { detail: 'boom' } } })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Something went wrong. Try again.')

    await userEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((call) => call === USERS).length).toBeGreaterThan(1)
    })
  })
})

describe('Admin — Rules (FR-D2, FR-T5)', () => {
  async function openRules(routes: MockRoutes = {}) {
    const rendered = renderAdmin(routes, '/admin?tab=rules')
    await screen.findByLabelText('Look-ahead N')
    return rendered
  }

  it('binds to the stored values and shows every default beside them', async () => {
    await openRules()

    expect(screen.getByLabelText('Look-ahead N')).toHaveValue(3)
    expect(screen.getByLabelText('Grace days G')).toHaveValue(7)
    expect(screen.getByLabelText('Unwatched days D')).toHaveValue(21)
    expect(screen.getByLabelText('Preferred resolution')).toHaveValue('1080p')
    expect(screen.getByLabelText('Fallback resolution')).toHaveValue('720p')
    expect(screen.getByLabelText('Subtitle language')).toHaveValue('en')
    expect(screen.getByLabelText('Audio language')).toHaveValue('ja')

    expect(screen.getByText('SubsPlease')).toBeInTheDocument()
    expect(screen.getByText('Erai-raws')).toBeInTheDocument()

    // The defaults are hints, not values: N's default is 2 while it holds 3.
    expect(screen.getByText('Default: 2')).toBeInTheDocument()
    expect(screen.getByText('Default: none')).toBeInTheDocument()

    // The overrides are listed, and not editable until M16.
    const overrides = screen.getByRole('table', { name: 'Per-show overrides' })
    expect(within(overrides).getByText('Sousou no Frieren')).toBeInTheDocument()
    expect(within(overrides).getByText('Tsundere-Raws')).toBeInTheDocument()
  })

  it('saves only the keys that changed', async () => {
    const { fetchMock } = await openRules()

    fireEvent.change(screen.getByLabelText('Look-ahead N'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('Subtitle language'), { target: { value: 'es' } })
    expect(screen.getByText('2 unsaved changes')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Save rules' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(SAVE_SETTINGS)
    })
    expect(bodyOf(fetchMock, 'PUT', '/api/settings')).toEqual({
      look_ahead_n: 4,
      sub_lang: 'es',
    })
  })

  it('will not save when nothing has changed', async () => {
    await openRules()
    expect(screen.getByRole('button', { name: 'Save rules' })).toBeDisabled()
  })

  it('adds and removes a preferred group, and sends the whole list', async () => {
    const { fetchMock } = await openRules()

    await userEvent.click(screen.getByRole('button', { name: 'Remove Erai-raws' }))
    await userEvent.type(screen.getByLabelText('Preferred release groups'), 'Judas')
    await userEvent.click(screen.getByRole('button', { name: 'Add group' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save rules' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(SAVE_SETTINGS)
    })
    expect(bodyOf(fetchMock, 'PUT', '/api/settings')).toEqual({
      preferred_groups: ['SubsPlease', 'Judas'],
    })
  })

  it('puts a field back to its default', async () => {
    await openRules()

    await userEvent.click(
      within(fieldOf('Look-ahead N')).getByRole('button', { name: 'Reset to default' }),
    )

    expect(screen.getByLabelText('Look-ahead N')).toHaveValue(2)
    expect(screen.getByText('1 unsaved change')).toBeInTheDocument()
  })

  it('renders a 422 under the field the server named', async () => {
    await openRules({
      [SAVE_SETTINGS]: {
        status: 422,
        body: {
          detail: [
            {
              loc: ['body', 'sub_lang'],
              msg: 'not a language code this server knows',
              type: 'value_error',
            },
          ],
        },
      },
    })

    fireEvent.change(screen.getByLabelText('Subtitle language'), { target: { value: 'xqz' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save rules' }))

    const field = fieldOf('Subtitle language')
    expect(await within(field).findByRole('alert')).toHaveTextContent(
      'not a language code this server knows',
    )
    // The one message goes under its field, not doubled at the foot of the form.
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('carries the ranges the server validates against, and says them', async () => {
    await openRules()

    // The numbers `arc/services/settings.py` checks: MAX_LOOK_AHEAD for N,
    // MAX_DAYS (365, not retention/rules.py's 3650 clamp) for G and D.
    const bounds: [string, string][] = [
      ['Look-ahead N', '10'],
      ['Grace days G', '365'],
      ['Unwatched days D', '365'],
    ]
    for (const [label, max] of bounds) {
      const input = screen.getByLabelText(label)
      expect(input).toHaveAttribute('min', '0')
      expect(input).toHaveAttribute('max', max)
      // The sentence under the field is generated from the same constant, so
      // it cannot drift from what the input will actually accept.
      expect(within(fieldOf(label)).getByText(new RegExp(`Allowed: 0–${max}\\.`))).toBeVisible()
    }
  })

  it('keeps the browser from sending a number outside its range', async () => {
    const { fetchMock } = await openRules()

    // N is capped at 10 server-side; the input says so too, so the form never
    // leaves for a value the server would only bounce back.
    fireEvent.change(screen.getByLabelText('Look-ahead N'), { target: { value: '99' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save rules' }))

    expect(requestsMade(fetchMock)).not.toContain(SAVE_SETTINGS)

    // And the same for a day count now that G and D stop at 365.
    fireEvent.change(screen.getByLabelText('Grace days G'), { target: { value: '400' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save rules' }))

    expect(requestsMade(fetchMock)).not.toContain(SAVE_SETTINGS)
  })

  it('rebinds to what the server answers with, and says so', async () => {
    await openRules({
      [SAVE_SETTINGS]: { body: { ...SETTINGS, values: { ...SETTINGS.values, look_ahead_n: 4 } } },
    })

    fireEvent.change(screen.getByLabelText('Look-ahead N'), { target: { value: '4' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save rules' }))

    expect(await screen.findByText('Rules saved.')).toBeInTheDocument()
    expect(screen.getByLabelText('Look-ahead N')).toHaveValue(4)
    expect(screen.getByRole('button', { name: 'Save rules' })).toBeDisabled()
  })
})

describe('Admin — Jobs (FR-D3)', () => {
  async function openJobs(routes: MockRoutes = {}) {
    const rendered = renderAdmin(routes, '/admin?tab=jobs')
    await screen.findByRole('table', { name: 'Jobs' })
    return rendered
  }

  it('shows the counts, the worker, and one row per job', async () => {
    await openJobs()

    expect(screen.getByText('pending: 4')).toBeInTheDocument()
    expect(screen.getByText('done: 812')).toBeInTheDocument()
    expect(screen.getByText('worker alive')).toBeInTheDocument()
    expect(screen.getByText(/last heartbeat/)).toBeInTheDocument()
    expect(screen.getByText(/compute_wants \(1\)/)).toBeInTheDocument()

    const table = screen.getByRole('table', { name: 'Jobs' })
    expect(within(table).getByText('transcode')).toBeInTheDocument()
    expect(within(table).getByText('3/5')).toBeInTheDocument()
  })

  it('truncates a long error until it is expanded', async () => {
    await openJobs()

    expect(screen.queryByText(LONG_ERROR)).not.toBeInTheDocument()
    expect(screen.getByText(/ffmpeg exited with 1/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Show more' }))
    expect(screen.getByText(LONG_ERROR)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Show less' }))
    expect(screen.queryByText(LONG_ERROR)).not.toBeInTheDocument()
  })

  it('retries a failed job and cancels a pending one', async () => {
    const { fetchMock } = await openJobs({
      'POST /api/jobs/102/retry': { body: { ...FAILED_JOB, status: 'pending' } },
      'POST /api/jobs/101/cancel': { body: { ...PENDING_JOB, status: 'cancelled' } },
    })

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/jobs/102/retry')
    })

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/jobs/101/cancel')
    })
  })

  it('offers neither action for a running job', async () => {
    await openJobs()

    const table = screen.getByRole('table', { name: 'Jobs' })
    const row = within(table).getByText('search_release').closest('tr')
    expect(row).not.toBeNull()
    expect(within(row as HTMLElement).queryByRole('button')).not.toBeInTheDocument()
  })

  it('renders the server’s 409 when the job moved under the click', async () => {
    await openJobs({
      'POST /api/jobs/102/retry': {
        status: 409,
        body: { detail: 'only a failed or cancelled job can be retried' },
      },
    })

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'only a failed or cancelled job can be retried',
    )
  })

  it('puts the chosen filters in the request', async () => {
    const { fetchMock } = await openJobs({
      'GET /api/jobs?status=failed&limit=50&offset=0': { body: [FAILED_JOB] },
    })

    await userEvent.selectOptions(screen.getByLabelText('Status'), 'failed')

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('GET /api/jobs?status=failed&limit=50&offset=0')
    })
  })
})

describe('Admin — Storage (FR-D3, FR-T4)', () => {
  async function openStorage(routes: MockRoutes = {}) {
    const rendered = renderAdmin(routes, '/admin?tab=storage')
    await screen.findByRole('progressbar')
    return rendered
  }

  it('shows how full the disk is and what is retained', async () => {
    await openStorage()

    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '62')
    expect(screen.getByText(/used of/)).toBeInTheDocument()
    expect(screen.getByText('Episodes retained').nextElementSibling).toHaveTextContent('27')
    expect(screen.getByText('Renditions')).toBeInTheDocument()
  })

  it('lists what the next sweep would delete, and why', async () => {
    await openStorage()

    const table = await screen.findByRole('table', { name: 'Retention preview' })
    expect(within(table).getByText('Sousou no Frieren')).toBeInTheDocument()
    expect(within(table).getByText(RETENTION_REASON)).toBeInTheDocument()
    expect(screen.getByText(/1 episode would be deleted/)).toBeInTheDocument()
  })

  it('queues a sweep on demand', async () => {
    const { fetchMock } = await openStorage({
      'POST /api/retention/sweep': { status: 202, body: { ...PENDING_JOB, id: 501 } },
    })

    await userEvent.click(screen.getByRole('button', { name: 'Run retention sweep now' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/retention/sweep')
    })
    expect(await screen.findByText(/Sweep queued as job 501/)).toBeInTheDocument()
  })

  it('asks before deleting an episode’s files, then queues the deletion', async () => {
    const { fetchMock } = await openStorage({
      'POST /api/episodes/9005/delete-files': { status: 202, body: PENDING_JOB },
    })

    await userEvent.click(await screen.findByRole('button', { name: 'Delete files' }))
    expect(requestsMade(fetchMock)).not.toContain('POST /api/episodes/9005/delete-files')
    expect(screen.getByText('Delete Sousou no Frieren episode 5?')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/episodes/9005/delete-files')
    })
    expect(await screen.findByText('Deletion queued.')).toBeInTheDocument()
  })

  it('re-fetches an episode without asking (FR-T4)', async () => {
    const { fetchMock } = await openStorage({
      'POST /api/episodes/9005/search': { status: 202, body: PENDING_JOB },
    })

    await userEvent.click(await screen.findByRole('button', { name: 'Re-fetch' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/episodes/9005/search')
    })
    expect(await screen.findByText('Search queued.')).toBeInTheDocument()
  })

  it('shows what the offline catalogue import loaded, and how old it is (M15.5)', async () => {
    await openStorage()

    expect(await screen.findByText('Anime database (manami)')).toBeInTheDocument()
    expect(screen.getByText(/2026-09-07 · 41,537 rows · imported 3 days ago/)).toBeInTheDocument()

    expect(screen.getByText('Id map (Fribb)')).toBeInTheDocument()
    // The ETag is shown short: the first 12 characters and no more.
    expect(screen.getByText(/^b3c1d9f4a77e · 32,281 rows/)).toBeInTheDocument()

    expect(screen.getByText('Next run: Mondays 03:30 UTC (weekly)')).toBeInTheDocument()
    expect(screen.queryByText(/Import is stale/)).not.toBeInTheDocument()
  })

  it('warns when the offline import is stale, and says how to fix it', async () => {
    await openStorage({ [OFFLINE_PATH]: { body: OFFLINE_STALE } })

    const warning = await screen.findByText(/Import is stale \(older than 14 days\)/)
    expect(warning).toHaveTextContent('or wait for Monday')
    expect(screen.getByText('python -m arc.cli import-catalogue')).toBeInTheDocument()
  })

  it('says so when the offline catalogue has never been imported', async () => {
    await openStorage({ [OFFLINE_PATH]: { body: OFFLINE_NEVER } })

    expect(
      await screen.findByText('Never imported. The worker imports it on first start.'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Anime database (manami)')).not.toBeInTheDocument()
  })
})

describe('Admin — Acquisition (FR-D3)', () => {
  async function openAcquisition(routes: MockRoutes = {}) {
    const rendered = renderAdmin(routes, '/admin?tab=acquisition')
    await screen.findByRole('heading', { name: 'Active wants' })
    return rendered
  }

  it('pauses acquisition and says what a pause does not stop', async () => {
    const { fetchMock } = await openAcquisition({
      'POST /api/acquisition/pause': { body: { paused: true } },
    })

    expect(await screen.findByText('Acquisition is running.')).toBeInTheDocument()
    expect(screen.getByText(/Downloads already running still finish/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Pause acquisition' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/acquisition/pause')
    })
  })

  it('resumes when it is paused', async () => {
    const { fetchMock } = await openAcquisition({
      [STATUS_PATH]: { body: PAUSED },
      'POST /api/acquisition/resume': { body: { paused: false } },
    })

    await userEvent.click(await screen.findByRole('button', { name: 'Resume acquisition' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('POST /api/acquisition/resume')
    })
  })

  it('lists every live want with its episode state', async () => {
    await openAcquisition()

    const table = await screen.findByRole('table', { name: 'Active wants' })
    expect(within(table).getByText('leah@example.com')).toBeInTheDocument()
    expect(within(table).getByText('Downloading')).toBeInTheDocument()
    expect(within(table).getByText('Unavailable')).toBeInTheDocument()
    expect(
      within(table).getByText('no release matched the rules after 6 attempts'),
    ).toBeInTheDocument()
  })

  it('shows qBittorrent’s version and its torrents', async () => {
    await openAcquisition()

    expect(await screen.findByText('reachable')).toBeInTheDocument()
    expect(screen.getByText('version v4.6.5')).toBeInTheDocument()

    const table = await screen.findByRole('table', { name: 'Torrents' })
    expect(within(table).getByText(/Sousou no Frieren - 06/)).toBeInTheDocument()
    expect(within(table).getByText('42%')).toBeInTheDocument()
  })

  it('renders an unreachable qBittorrent as an answer, not a failed page', async () => {
    await openAcquisition({ [QBIT_PATH]: { body: QBIT_DOWN } })

    expect(await screen.findByText('unreachable')).toBeInTheDocument()
    expect(screen.getByText('connection refused: qbittorrent:8080')).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Torrents' })).not.toBeInTheDocument()
  })
})
