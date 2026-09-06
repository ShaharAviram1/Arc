import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { scheduleQueryKey } from '@/lib/schedule'
import { Schedule } from '@/pages/Schedule'
import {
  APOTHECARY,
  EMPTY_SCHEDULE,
  FRIEREN,
  FRIEREN_SPECIAL,
  listEntry,
  SCHEDULE_PAGE,
} from '@/test/animeFixtures'
import {
  callTo,
  jsonBodyOf,
  mockApi,
  requestsMade,
  TEST_USER,
  type MockRoutes,
} from '@/test/apiMock'

/** A Wednesday, 12:00 in Europe/Berlin — the fixture's timezone. */
const WEDNESDAY = new Date('2026-09-09T10:00:00Z')

function renderSchedule(path = '/schedule', client: QueryClient = createQueryClient()) {
  const router = createMemoryRouter(
    [
      { path: '/schedule', element: <Schedule /> },
      { path: '/anime/:id', element: <p>show page</p> },
    ],
    { initialEntries: [path] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

/**
 * `mockApi`, with every GET for `path` after the first left hanging. That is
 * exactly the window a cache patch exists for: the list write has answered,
 * the refetch that would confirm it has not.
 */
function mockApiHoldingRefetch(path: string, routes: MockRoutes) {
  const fetchMock = mockApi(routes)
  const serve = fetchMock.getMockImplementation() as (
    input: string | URL | Request,
    init?: RequestInit,
  ) => Promise<Response>
  let served = 0

  fetchMock.mockImplementation((input, init) => {
    if (input === path && (init?.method ?? 'GET').toUpperCase() === 'GET' && served++ > 0) {
      return new Promise<Response>(() => undefined)
    }
    return serve(input, init)
  })

  return fetchMock
}

function dayColumn(label: string): HTMLElement {
  return screen.getByRole('region', { name: label })
}

beforeEach(() => {
  // Only `Date` is faked: react-query and user-event still need real timers.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(WEDNESDAY)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Schedule', () => {
  it('renders the season, the timezone note and seven weekday columns', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()

    expect(await screen.findByText('Fall 2026')).toBeInTheDocument()
    expect(screen.getByText('Times in Europe/Berlin')).toBeInTheDocument()

    const headings = screen.getAllByRole('heading', { level: 2 })
    expect(headings.map((heading) => heading.textContent)).toEqual([
      'Monday',
      'Tuesday',
      'Wednesday· today',
      'Thursday',
      'Friday',
      'Saturday',
      'Sunday',
    ])
  })

  it('puts each show under the day the server grouped it into', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const tuesday = within(dayColumn('Tuesday'))
    expect(tuesday.getByRole('link', { name: FRIEREN.title.preferred })).toHaveAttribute(
      'href',
      `/anime/${FRIEREN.id}`,
    )
    expect(tuesday.getByText('18:30 · Ep 5')).toBeInTheDocument()
    // Every row carries its own add-to-list control (FR-C3).
    expect(tuesday.getByRole('combobox')).toHaveValue('watching')

    const thursday = within(dayColumn('Thursday'))
    expect(thursday.getByText(APOTHECARY.title.preferred)).toBeInTheDocument()
    // The Apothecary's slot came from MAL, so the time carries "est." (FR-C6).
    expect(thursday.getByRole('listitem')).toHaveTextContent('22:00 est. · Ep 2')

    expect(within(dayColumn('Monday')).getByText('Nothing airing')).toBeInTheDocument()
  })

  it('marks an estimated air time and leaves a real one unmarked', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    // One row in the whole grid was synthesised from a broadcast slot.
    const estimated = screen.getAllByText('est.')
    expect(estimated).toHaveLength(1)
    expect(estimated[0]).toHaveAttribute('title', 'Estimated from the broadcast slot')
    expect(estimated[0]).toHaveClass('italic')
    expect(within(dayColumn('Thursday')).getByText('est.')).toBeInTheDocument()

    // Frieren's time came from AniList, so it stays plain.
    expect(within(dayColumn('Tuesday')).queryByText('est.')).not.toBeInTheDocument()
    expect(within(dayColumn('Tuesday')).getByRole('listitem')).toHaveTextContent('18:30 · Ep 5')
  })

  it('highlights today in the schedule’s timezone, not the browser’s', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    expect(dayColumn('Wednesday')).toHaveAttribute('data-today', 'true')
    expect(dayColumn('Tuesday')).not.toHaveAttribute('data-today')
  })

  it('moves the highlight when the viewer’s zone is already on the next day', async () => {
    // 23:00 UTC on that Wednesday is Thursday in Tokyo.
    vi.setSystemTime(new Date('2026-09-09T23:00:00Z'))
    mockApi({
      'GET /api/schedule': { body: { ...SCHEDULE_PAGE, timezone: 'Asia/Tokyo' } },
    })

    renderSchedule()
    await screen.findByText('Fall 2026')

    expect(dayColumn('Thursday')).toHaveAttribute('data-today', 'true')
    expect(dayColumn('Wednesday')).not.toHaveAttribute('data-today')
  })

  it('marks followed shows and leaves the rest plain', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const followed = document.body.querySelectorAll('[data-following="true"]')
    expect(followed).toHaveLength(1)
    expect(followed[0]?.className).toContain('border-l-[var(--arc-accent)]')
    expect(followed[0]?.textContent).toContain(FRIEREN.title.preferred)

    // The Apothecary is not followed, so it gets no accent edge.
    const thursdayRow = within(dayColumn('Thursday')).getByRole('listitem')
    expect(thursdayRow).not.toHaveAttribute('data-following')
  })

  it('steps to the previous season, updating the URL and refetching', async () => {
    const fetchMock = mockApi({
      'GET /api/schedule': { body: SCHEDULE_PAGE },
      'GET /api/schedule?year=2026&season=SUMMER': {
        body: { ...EMPTY_SCHEDULE, year: 2026, season: 'SUMMER' },
      },
    })

    const router = renderSchedule()
    await screen.findByText('Fall 2026')

    await userEvent.click(screen.getByRole('button', { name: 'Previous season' }))

    await waitFor(() => {
      expect(router.state.location.search).toBe('?year=2026&season=SUMMER')
    })
    expect(await screen.findByText('Summer 2026')).toBeInTheDocument()
    expect(requestsMade(fetchMock)).toContain('GET /api/schedule?year=2026&season=SUMMER')
  })

  it('steps to the next season', async () => {
    const fetchMock = mockApi({
      'GET /api/schedule': { body: SCHEDULE_PAGE },
      'GET /api/schedule?year=2027&season=WINTER': {
        body: { ...EMPTY_SCHEDULE, year: 2027, season: 'WINTER' },
      },
    })

    const router = renderSchedule()
    await screen.findByText('Fall 2026')

    await userEvent.click(screen.getByRole('button', { name: 'Next season' }))

    await waitFor(() => {
      expect(router.state.location.search).toBe('?year=2027&season=WINTER')
    })
    expect(requestsMade(fetchMock)).toContain('GET /api/schedule?year=2027&season=WINTER')
  })

  it('reads the season out of the URL on arrival', async () => {
    const fetchMock = mockApi({
      'GET /api/schedule?year=2026&season=SUMMER': {
        body: { ...SCHEDULE_PAGE, year: 2026, season: 'SUMMER' },
      },
    })

    renderSchedule('/schedule?year=2026&season=SUMMER')

    expect(await screen.findByText('Summer 2026')).toBeInTheDocument()
    expect(requestsMade(fetchMock)).toEqual(['GET /api/schedule?year=2026&season=SUMMER'])
  })

  it('keeps the unscheduled list collapsed until it is asked for', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const toggle = screen.getByRole('button', {
      name: 'Show Movies, OVAs and unscheduled (1)',
    })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(FRIEREN_SPECIAL.title.preferred)).not.toBeInTheDocument()

    await userEvent.click(toggle)

    expect(screen.getByText(FRIEREN_SPECIAL.title.preferred)).toBeInTheDocument()
    expect(screen.getByText('Time unknown')).toBeInTheDocument()
    const hide = screen.getByRole('button', { name: 'Hide Movies, OVAs and unscheduled (1)' })
    expect(hide).toHaveAttribute('aria-expanded', 'true')

    await userEvent.click(hide)
    expect(screen.queryByText(FRIEREN_SPECIAL.title.preferred)).not.toBeInTheDocument()
  })

  it('says so when the sweep has not cached this season yet', async () => {
    mockApi({ 'GET /api/schedule': { body: EMPTY_SCHEDULE } })

    renderSchedule()

    expect(
      await screen.findByText(
        'Nothing cached for this season yet — the catalogue sweep runs daily.',
      ),
    ).toBeInTheDocument()
  })

  it('names the outage when both catalogue sources are down', async () => {
    mockApi({
      'GET /api/schedule': { status: 502, body: { detail: 'catalogue is unavailable' } },
    })

    renderSchedule()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The catalogue is unavailable right now. Try again in a few minutes.',
    )
  })

  it('keeps a generic message for a failure that is not an outage', async () => {
    mockApi({ 'GET /api/schedule': { status: 500, body: { detail: 'boom' } } })

    renderSchedule()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the schedule.')
  })
})

describe('Schedule list writes', () => {
  it('keeps the row on its new status while the schedule is still refetching', async () => {
    const fetchMock = mockApiHoldingRefetch('/api/schedule', {
      'GET /api/schedule': { body: SCHEDULE_PAGE },
      [`PUT /api/list/${FRIEREN.id}`]: { body: listEntry({ status: 'completed' }) },
    })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const row = within(dayColumn('Tuesday')).getByRole('listitem')
    const select = within(row).getByRole('combobox')
    expect(select).toHaveValue('watching')

    await userEvent.selectOptions(select, 'completed')

    await waitFor(() => {
      expect(select).toHaveValue('completed')
    })
    // Completed is not a followed status, so the accent edge goes with it.
    expect(row).not.toHaveAttribute('data-following')
    // And none of that came from the server: the refetch is still in flight.
    expect(requestsMade(fetchMock).filter((made) => made === 'GET /api/schedule')).toHaveLength(2)
  })

  it('clears the accent edge as soon as a row leaves the list', async () => {
    mockApiHoldingRefetch('/api/schedule', {
      'GET /api/schedule': { body: SCHEDULE_PAGE },
      [`DELETE /api/list/${FRIEREN.id}`]: { status: 204 },
    })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const row = within(dayColumn('Tuesday')).getByRole('listitem')
    expect(row).toHaveAttribute('data-following', 'true')

    await userEvent.selectOptions(within(row).getByRole('combobox'), '')

    await waitFor(() => {
      expect(within(row).getByRole('combobox')).toHaveValue('')
    })
    expect(row).not.toHaveAttribute('data-following')
    expect(document.body.querySelectorAll('[data-following="true"]')).toHaveLength(0)
  })
})

describe('Schedule timezone control', () => {
  const TOKYO_USER = { ...TEST_USER, timezone: 'Asia/Tokyo' }

  /** The header, before anyone has touched the control. */
  async function renderWithZone(routes: MockRoutes = {}) {
    const fetchMock = mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE }, ...routes })
    renderSchedule()
    await screen.findByText('Fall 2026')
    return fetchMock
  }

  function timezoneSelect(): HTMLElement {
    return screen.getByLabelText('Timezone')
  }

  it('keeps the select hidden until "Change" asks for it', async () => {
    await renderWithZone()

    expect(screen.getByText('Times in Europe/Berlin')).toBeInTheDocument()
    expect(screen.queryByLabelText('Timezone')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Change' }))

    expect(timezoneSelect()).toHaveValue('Europe/Berlin')
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    // "Change" gives way to the open control rather than sitting next to it.
    expect(screen.queryByRole('button', { name: 'Change' })).not.toBeInTheDocument()
  })

  it('saves the chosen zone and takes the header from the user it gets back', async () => {
    const fetchMock = await renderWithZone({ 'PATCH /api/users/me': { body: TOKYO_USER } })

    await userEvent.click(screen.getByRole('button', { name: 'Change' }))
    await userEvent.selectOptions(timezoneSelect(), 'Asia/Tokyo')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Times in Asia/Tokyo')).toBeInTheDocument()
    // Saved, so the control closes again.
    expect(screen.queryByLabelText('Timezone')).not.toBeInTheDocument()

    expect(requestsMade(fetchMock)).toContain('PATCH /api/users/me')
    expect(jsonBodyOf(callTo(fetchMock, '/api/users/me'))).toEqual({ timezone: 'Asia/Tokyo' })
  })

  it('re-fetches the schedule, because the grouping is server-side', async () => {
    const fetchMock = await renderWithZone({ 'PATCH /api/users/me': { body: TOKYO_USER } })

    await userEvent.click(screen.getByRole('button', { name: 'Change' }))
    await userEvent.selectOptions(timezoneSelect(), 'Asia/Tokyo')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((made) => made === 'GET /api/schedule')).toHaveLength(2)
    })
  })

  it('shows the server’s complaint about a zone it will not take', async () => {
    await renderWithZone({
      'PATCH /api/users/me': { status: 422, body: { detail: 'unknown timezone' } },
    })

    await userEvent.click(screen.getByRole('button', { name: 'Change' }))
    await userEvent.selectOptions(timezoneSelect(), 'Asia/Tokyo')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('unknown timezone')
    // The zone did not change, and the control stays open to be corrected.
    expect(screen.getByText('Times in Europe/Berlin')).toBeInTheDocument()
    expect(timezoneSelect()).toBeInTheDocument()
  })

  it('closes on Cancel without asking the server anything', async () => {
    const fetchMock = await renderWithZone({ 'PATCH /api/users/me': { body: TOKYO_USER } })

    await userEvent.click(screen.getByRole('button', { name: 'Change' }))
    await userEvent.selectOptions(timezoneSelect(), 'Asia/Tokyo')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByLabelText('Timezone')).not.toBeInTheDocument()
    expect(screen.getByText('Times in Europe/Berlin')).toBeInTheDocument()
    expect(requestsMade(fetchMock)).toEqual(['GET /api/schedule'])

    // And re-opening starts from the saved zone, not the abandoned choice.
    await userEvent.click(screen.getByRole('button', { name: 'Change' }))
    expect(timezoneSelect()).toHaveValue('Europe/Berlin')
  })
})

describe('Schedule "today" while the tab stays open', () => {
  /**
   * The cache is seeded so the page renders with data on the first pass: this
   * test drives `setInterval` by hand, and a pending query would need the real
   * one to resolve.
   */
  function renderSeeded() {
    const client = createQueryClient()
    client.setQueryData(scheduleQueryKey(), SCHEDULE_PAGE)
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })
    renderSchedule('/schedule', client)
  }

  it('moves the highlight when the clock rolls past midnight', () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    // 23:59 in Europe/Berlin, the fixture's zone.
    vi.setSystemTime(new Date('2026-09-09T21:59:00Z'))

    renderSeeded()
    expect(dayColumn('Wednesday')).toHaveAttribute('data-today', 'true')

    act(() => {
      vi.setSystemTime(new Date('2026-09-09T22:01:00Z'))
      vi.advanceTimersByTime(60_000)
    })

    expect(dayColumn('Thursday')).toHaveAttribute('data-today', 'true')
    expect(dayColumn('Wednesday')).not.toHaveAttribute('data-today')
  })

  it('stops the interval when the page goes away', () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(WEDNESDAY)

    renderSeeded()
    expect(vi.getTimerCount()).toBe(1)

    cleanup()
    expect(vi.getTimerCount()).toBe(0)
  })
})
