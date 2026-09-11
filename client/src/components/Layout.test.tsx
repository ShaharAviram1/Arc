import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { PHONE_MEDIA_QUERY } from '@/lib/media'
import { createQueryClient } from '@/lib/queryClient'
import { mockApi, requestsMade, TEST_ADMIN, TEST_USER, type MockResponse } from '@/test/apiMock'

const HEALTH = { body: { status: 'ok', version: '0.1.0', env: 'test' } }
const REVIEW_SUMMARY = 'GET /api/review/summary'
const ACQUISITION_STATUS = 'GET /api/acquisition/status'
const PAUSED_NOTE = 'Acquisition paused'
/** The mark is a link home; the image inside it is decorative. */
const LOGO_LABEL = 'Arc — Watch Now'

/** An acquisition status body with the flag set however we say. */
function status(paused: boolean) {
  return { body: { paused, active_wants: 4, searching: 1, downloading: 0 } }
}

/**
 * The phone chrome is a different set of elements, not a restyled toolbar, so
 * it is chosen in JavaScript. Tests say which viewport they are in by stubbing
 * the media query the shell asks about; `vi.unstubAllGlobals` in `afterEach`
 * puts the desktop default back.
 */
function setViewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query === PHONE_MEDIA_QUERY,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }))
}

interface RenderOptions {
  me?: typeof TEST_USER
  review?: MockResponse
  acquisition?: MockResponse
  path?: string
  phone?: boolean
}

/** The signed-in app, with whatever the shell's own queries should answer. */
function renderShell({
  me = TEST_USER,
  review,
  acquisition,
  path = '/',
  phone,
}: RenderOptions = {}) {
  if (phone !== undefined) setViewport(phone)
  const fetchMock = mockApi({
    'GET /api/auth/me': { body: me },
    'GET /api/health': HEALTH,
    // Watch Now reads the viewer's list to build its hero; answered here so a
    // shell test is not waiting out a retry on a 404 it does not care about.
    'GET /api/list': { body: [] },
    [REVIEW_SUMMARY]: review ?? { body: { pending: 0 } },
    ...(acquisition ? { [ACQUISITION_STATUS]: acquisition } : {}),
  })
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { fetchMock, router }
}

/** The avatar, once the session has resolved. */
function avatar() {
  return screen.findByRole('button', { name: 'Account' })
}

async function openAccountMenu() {
  const user = userEvent.setup()
  await user.click(await avatar())
  return user
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Toolbar', () => {
  it('shows the mark and the three destinations, with the current one marked', async () => {
    renderShell()

    expect(await screen.findByRole('link', { name: LOGO_LABEL })).toBeInTheDocument()
    const nav = screen.getByRole('navigation', { name: 'Main' })
    for (const label of ['Watch Now', 'Browse', 'Schedule']) {
      expect(nav).toContainElement(screen.getByRole('link', { name: label }))
    }
    // react-router marks the active NavLink; the pill is that, in CSS.
    expect(screen.getByRole('link', { name: 'Watch Now' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Browse' })).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('link', { name: 'Schedule' })).not.toHaveAttribute('aria-current')
  })

  it('makes the mark the way back to Watch Now', async () => {
    const { router } = renderShell({ path: '/schedule' })

    const logo = await screen.findByRole('link', { name: LOGO_LABEL })
    expect(logo).toHaveAttribute('href', '/')
    // The image is decorative: the link is named once, not twice.
    const image = logo.querySelector('img')
    expect(image).toHaveAttribute('src', '/arc-logo.png')
    expect(image).toHaveAttribute('alt', '')
    expect(screen.queryByRole('img', { name: 'Arc' })).not.toBeInTheDocument()

    await userEvent.click(logo)
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/')
    })
  })

  it('marks Schedule as the current place while it is on screen', async () => {
    renderShell({ path: '/schedule' })

    expect(await screen.findByRole('link', { name: 'Schedule' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(screen.getByRole('link', { name: 'Watch Now' })).not.toHaveAttribute('aria-current')
  })

  it('keeps Recommendations out of the nav', async () => {
    renderShell()

    await screen.findByRole('link', { name: 'Watch Now' })
    expect(screen.queryByRole('link', { name: 'Recommendations' })).not.toBeInTheDocument()
  })

  it('sends a typed query to Browse', async () => {
    const { router } = renderShell()

    const user = userEvent.setup()
    await user.type(await screen.findByRole('searchbox', { name: 'Search' }), 'frieren')

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/search')
    })
    expect(router.state.location.search).toBe('?q=frieren')
  })

  it('fills the field from the URL when Browse is arrived at directly', async () => {
    renderShell({ path: '/search?q=spice' })

    expect(await screen.findByRole('searchbox', { name: 'Search' })).toHaveValue('spice')
  })
})

describe('Account menu', () => {
  it('stays shut until the avatar is used', async () => {
    renderShell()

    expect(await avatar()).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('link', { name: 'MyAnimeList' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
  })

  it('holds everything that is not a daily destination', async () => {
    renderShell()
    await openAccountMenu()

    expect(screen.getByRole('link', { name: 'My List' })).toHaveAttribute('href', '/list')
    expect(screen.getByRole('link', { name: 'MyAnimeList' })).toHaveAttribute('href', '/mal')
    expect(screen.getByRole('link', { name: 'Review' })).toHaveAttribute('href', '/review')
    expect(screen.getByText(TEST_USER.email)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument()
  })

  it('wears the account initial', async () => {
    renderShell()

    expect(await avatar()).toHaveTextContent('V')
  })

  it('offers Admin to an admin and nobody else', async () => {
    renderShell({ me: TEST_ADMIN })
    await openAccountMenu()

    expect(screen.getByRole('link', { name: 'Admin' })).toHaveAttribute('href', '/admin')
  })

  it('closes when a destination in it is chosen', async () => {
    renderShell()
    const user = await openAccountMenu()

    await user.click(screen.getByRole('link', { name: 'MyAnimeList' }))

    await waitFor(() => {
      expect(screen.queryByRole('link', { name: 'MyAnimeList' })).not.toBeInTheDocument()
    })
  })

  it('opens onto its first item and moves on the arrow keys', async () => {
    renderShell()
    const user = await openAccountMenu()

    expect(screen.getByRole('link', { name: 'My List' })).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('link', { name: 'MyAnimeList' })).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('link', { name: 'My List' })).toHaveFocus()
    // Wraps, rather than stopping dead at the top.
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('button', { name: 'Log out' })).toHaveFocus()
  })

  it('closes on Escape and hands focus back to the avatar', async () => {
    renderShell()
    const user = await openAccountMenu()

    await user.keyboard('{Escape}')

    expect(screen.queryByRole('link', { name: 'MyAnimeList' })).not.toBeInTheDocument()
    expect(await avatar()).toHaveFocus()
  })
})

describe('Account menu review badge', () => {
  it('shows a count pill on the Review entry when files are waiting', async () => {
    renderShell({ review: { body: { pending: 3 } } })
    await openAccountMenu()

    const pill = screen.getByLabelText('3 files need review')
    expect(pill).toHaveTextContent('3')
    // It hangs off the menu entry rather than floating somewhere on its own.
    expect(screen.getByRole('link', { name: /Review/ })).toContainElement(pill)
  })

  it('says it in the singular for a single file', async () => {
    renderShell({ review: { body: { pending: 1 } } })
    await openAccountMenu()

    expect(screen.getByLabelText('1 file needs review')).toHaveTextContent('1')
  })

  it('shows no pill when nothing is waiting', async () => {
    const { fetchMock } = renderShell({ review: { body: { pending: 0 } } })
    await openAccountMenu()

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(REVIEW_SUMMARY)
    })
    expect(screen.queryByLabelText(/need(s)? review/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Review' })).toHaveTextContent(/^Review$/)
  })

  it('shows no pill when the count cannot be fetched', async () => {
    const { fetchMock } = renderShell({ review: { status: 500, body: { detail: 'boom' } } })
    await openAccountMenu()

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

  it('asks for the count once per mount, whether or not the menu is opened', async () => {
    const { fetchMock } = renderShell({ review: { body: { pending: 2 } } })

    await avatar()
    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(REVIEW_SUMMARY)
    })
    expect(requestsMade(fetchMock).filter((call) => call === REVIEW_SUMMARY)).toHaveLength(1)
  })
})

describe('Acquisition pause note', () => {
  it('says so in the account menu while acquisition is paused', async () => {
    renderShell({ me: TEST_ADMIN, acquisition: status(true) })
    await openAccountMenu()

    const note = await screen.findByText(PAUSED_NOTE)
    // Announced when it appears mid-session, not only on a reload.
    expect(note).toHaveAttribute('role', 'status')
    // Above Log out, which is the last thing in the menu.
    expect(note.compareDocumentPosition(screen.getByRole('button', { name: 'Log out' }))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
  })

  it('says nothing while acquisition is running', async () => {
    const { fetchMock } = renderShell({ me: TEST_ADMIN, acquisition: status(false) })
    await openAccountMenu()

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(ACQUISITION_STATUS)
    })
    expect(screen.queryByText(PAUSED_NOTE)).not.toBeInTheDocument()
  })

  it('does not ask a non-admin, and never shows them the note', async () => {
    const { fetchMock } = renderShell({ acquisition: status(true) })
    await openAccountMenu()

    expect(requestsMade(fetchMock)).not.toContain(ACQUISITION_STATUS)
    expect(screen.queryByText(PAUSED_NOTE)).not.toBeInTheDocument()
  })

  it('says nothing when the status cannot be fetched', async () => {
    const { fetchMock } = renderShell({
      me: TEST_ADMIN,
      acquisition: { status: 500, body: { detail: 'boom' } },
    })
    await openAccountMenu()

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(ACQUISITION_STATUS)
    })
    expect(screen.queryByText(PAUSED_NOTE)).not.toBeInTheDocument()
    // `retry: false`: a status line is not worth hammering a failing endpoint.
    expect(requestsMade(fetchMock).filter((call) => call === ACQUISITION_STATUS)).toHaveLength(1)
  })
})

describe('Phone chrome', () => {
  it('puts the sections in a bottom tab bar instead of the toolbar', async () => {
    renderShell({ phone: true })

    const tabs = await screen.findByRole('navigation', { name: 'Sections' })
    for (const label of ['Watch Now', 'Browse', 'My List']) {
      expect(tabs).toContainElement(screen.getByRole('link', { name: label }))
    }
    expect(tabs).toContainElement(screen.getByRole('button', { name: 'More' }))
    expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
  })

  it('collapses the toolbar to the mark and a search control that expands', async () => {
    renderShell({ phone: true })

    const user = userEvent.setup()
    // The mark is the same way home on a phone as it is on the toolbar.
    expect(await screen.findByRole('link', { name: LOGO_LABEL })).toHaveAttribute('href', '/')
    expect(screen.queryByRole('searchbox', { name: 'Search' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Search' }))
    expect(screen.getByRole('searchbox', { name: 'Search' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('searchbox', { name: 'Search' })).not.toBeInTheDocument()
  })

  it('keeps Schedule in the More sheet, since the tab bar has no room for it', async () => {
    renderShell({ phone: true })

    const user = userEvent.setup()
    await screen.findByRole('navigation', { name: 'Sections' })
    // Not a fifth tab: the four tabs are unchanged.
    expect(screen.queryByRole('link', { name: 'Schedule' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'More' }))

    const schedule = screen.getByRole('link', { name: 'Schedule' })
    expect(schedule).toHaveAttribute('href', '/schedule')
    // First in the sheet, above the account items. ("My List" is also a tab,
    // so the account entry that is only ever in the sheet is the landmark.)
    expect(
      schedule.compareDocumentPosition(screen.getByRole('link', { name: 'MyAnimeList' })),
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('opens the account items in the More sheet, and closes it on Escape', async () => {
    renderShell({ me: TEST_ADMIN, phone: true, review: { body: { pending: 2 } } })

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'More' }))

    expect(screen.getByRole('link', { name: 'MyAnimeList' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Admin' })).toBeInTheDocument()
    expect(screen.getByLabelText('2 files need review')).toBeInTheDocument()
    expect(screen.getByText(TEST_ADMIN.email)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('link', { name: 'MyAnimeList' })).not.toBeInTheDocument()
  })

  it('does not render the phone chrome on a desktop width', async () => {
    renderShell({ phone: false })

    await screen.findByRole('navigation', { name: 'Main' })
    expect(screen.queryByRole('navigation', { name: 'Sections' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'More' })).not.toBeInTheDocument()
  })
})
