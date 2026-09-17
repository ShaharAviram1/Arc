import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import type { AnimeSummary } from '@/lib/anime'
import { scheduleQueryKey, type SchedulePage } from '@/lib/schedule'
import { Schedule } from '@/pages/Schedule'
import {
  APOTHECARY,
  EMPTY_SCHEDULE,
  FRIEREN,
  FRIEREN_SPECIAL,
  listEntry,
  SCHEDULE_PAGE,
  scheduleEntry,
} from '@/test/animeFixtures'
import {
  callTo,
  jsonBodyOf,
  mockApi,
  requestsMade,
  TEST_USER,
  type MockRoutes,
} from '@/test/apiMock'

/**
 * A Tuesday, 12:00 in Europe/Berlin — the fixture's timezone. The week it
 * falls in runs Mon 5 Oct to Sun 11 Oct, so the three-day window opens on
 * Tue 6, Wed 7, Thu 8, which is where both fixture shows air.
 *
 * October is Fall, which is the season `SCHEDULE_PAGE` carries: the dates and
 * the "Today" chip belong to the live week, so the fixture clock has to be
 * inside the fixture's own season for the page to be showing one.
 */
const TUESDAY = new Date('2026-10-06T10:00:00Z')

/** The day-and-date bar, and the two chevrons that move it. */
function dayBar(): HTMLElement {
  return screen.getByRole('group', { name: 'Days shown' })
}

function previousDay(): HTMLElement {
  return screen.getByRole('button', { name: 'Previous day' })
}

function nextDay(): HTMLElement {
  return screen.getByRole('button', { name: 'Next day' })
}

/** The weekday names on screen, in the order the window shows them. */
function daysShown(): (string | null)[] {
  return screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent)
}

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

/**
 * The Tuesday column holding one show the server pulled into this week because
 * it is on air, not because it is tagged Fall 2026 (`carried_over`, owner
 * 2026-09-13). The override is the season the show *started* in, which is what
 * the caveat names.
 */
function carriedOverPage(anime: Partial<AnimeSummary>): SchedulePage {
  return {
    ...SCHEDULE_PAGE,
    days: SCHEDULE_PAGE.days.map((day) =>
      day.weekday === 1
        ? {
            ...day,
            entries: [
              scheduleEntry(
                { ...FRIEREN, season_year: 2026, ...anime },
                {
                  air_time_local: '18:30',
                  next_episode: 5,
                  next_at: '2026-09-08T16:30:00Z',
                  carried_over: true,
                },
              ),
            ],
          }
        : day,
    ),
  }
}

beforeEach(() => {
  // Only `Date` is faked: react-query and user-event still need real timers.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(TUESDAY)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Schedule', () => {
  it('renders the season, the timezone note and three days starting today', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()

    expect(await screen.findByText('Fall 2026')).toBeInTheDocument()
    expect(screen.getByText('Times in Europe/Berlin')).toBeInTheDocument()

    // Today, tomorrow and the day after, each named with its own date
    // (owner, 2026-09-17). The other four days of the week are an arrow away.
    expect(daysShown()).toEqual(['Tue 6 OctToday', 'Wed 7 Oct', 'Thu 8 Oct'])
    expect(screen.queryByRole('region', { name: 'Monday' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Friday' })).not.toBeInTheDocument()
  })

  it('moves the window one day at a time with the bar’s arrows', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    await userEvent.click(nextDay())
    expect(daysShown()).toEqual(['Wed 7 Oct', 'Thu 8 Oct', 'Fri 9 Oct'])

    await userEvent.click(previousDay())
    expect(daysShown()).toEqual(['Tue 6 OctToday', 'Wed 7 Oct', 'Thu 8 Oct'])

    await userEvent.click(previousDay())
    expect(daysShown()).toEqual(['Mon 5 Oct', 'Tue 6 OctToday', 'Wed 7 Oct'])
  })

  it('stops at both ends of the week the server sent', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    // Tuesday is a day in, so both arrows are live to begin with.
    expect(previousDay()).toBeEnabled()
    expect(nextDay()).toBeEnabled()

    await userEvent.click(previousDay())
    expect(daysShown()[0]).toBe('Mon 5 Oct')
    expect(previousDay()).toBeDisabled()

    // Friday is as far as three columns reach: Saturday and Sunday are the
    // other two, and a fourth column of nothing is not what the arrow offers.
    for (let step = 0; step < 4; step += 1) {
      await userEvent.click(nextDay())
    }
    expect(daysShown()).toEqual(['Fri 9 Oct', 'Sat 10 Oct', 'Sun 11 Oct'])
    expect(nextDay()).toBeDisabled()
  })

  it('moves the window with the left and right arrow keys', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    fireEvent.keyDown(dayBar(), { key: 'ArrowRight' })
    expect(daysShown()).toEqual(['Wed 7 Oct', 'Thu 8 Oct', 'Fri 9 Oct'])

    fireEvent.keyDown(dayBar(), { key: 'ArrowLeft' })
    expect(daysShown()).toEqual(['Tue 6 OctToday', 'Wed 7 Oct', 'Thu 8 Oct'])
  })

  it('leaves the arrow keys to the list-status select', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const select = within(dayColumn('Tuesday')).getByRole('combobox')
    fireEvent.keyDown(select, { key: 'ArrowRight' })

    // The window has not moved: inside a field the arrows belong to the field.
    expect(daysShown()).toEqual(['Tue 6 OctToday', 'Wed 7 Oct', 'Thu 8 Oct'])
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

    expect(within(dayColumn('Wednesday')).getByText('Nothing airing')).toBeInTheDocument()
  })

  it('gives a row the whole title rather than clamping it', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    // Three columns are ~350px each, so the name fits: nothing truncates it
    // and nothing clamps it to a line count (owner, 2026-09-17).
    const title = within(dayColumn('Tuesday')).getByText(FRIEREN.title.preferred)
    expect(title.textContent).toBe(FRIEREN.title.preferred)
    expect(title.className).not.toMatch(/truncate|line-clamp|text-ellipsis/)

    // And the times are readable at arm's length rather than 12px.
    const slot = within(dayColumn('Tuesday')).getByText('18:30 · Ep 5')
    expect(slot.className).toContain('text-[15px]')
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

  it('says which season a carried-over show started in', async () => {
    mockApi({ 'GET /api/schedule': { body: carriedOverPage({ season: 'SPRING' }) } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const tuesday = within(dayColumn('Tuesday'))
    expect(tuesday.getByText(FRIEREN.title.preferred)).toBeInTheDocument()
    expect(tuesday.getByText('Since Spring 2026')).toBeInTheDocument()
    // The slot is unchanged: the caveat is a line of its own, not a suffix.
    expect(tuesday.getByText('18:30 · Ep 5')).toBeInTheDocument()
  })

  it('leaves the ordinary rows of the season without a caveat', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    // Frieren is tagged Fall 2023 in the fixtures and `carried_over` is false,
    // so the page says nothing: the flag is the server's answer, not a
    // comparison the client makes for itself.
    expect(screen.queryByText(/^Since /)).not.toBeInTheDocument()
  })

  it('says nothing about a show the catalogue gives no season', async () => {
    mockApi({
      'GET /api/schedule': { body: carriedOverPage({ season: null, season_year: null }) },
    })

    renderSchedule()
    await screen.findByText('Fall 2026')

    expect(within(dayColumn('Tuesday')).getByText(FRIEREN.title.preferred)).toBeInTheDocument()
    expect(screen.queryByText(/^Since /)).not.toBeInTheDocument()
  })

  it('marks today in the schedule’s timezone, not the browser’s', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const today = dayColumn('Tuesday')
    expect(today).toHaveAttribute('data-today', 'true')
    expect(within(today).getByText('Today')).toBeInTheDocument()
    // The accent underline and the label are the same ember, and today's
    // column is the only place on this page it appears.
    expect(today.querySelector('h2')?.innerHTML).toContain('--arc-ember')

    const tomorrow = dayColumn('Wednesday')
    expect(tomorrow).not.toHaveAttribute('data-today')
    expect(within(tomorrow).queryByText('Today')).not.toBeInTheDocument()
    expect(tomorrow.querySelector('h2')?.innerHTML).not.toContain('--arc-ember')
  })

  it('gives a browsed season weekday names alone — no dates, no today', async () => {
    // Summer 2026 is a catalogue browse: its grid is the shows of that season
    // by weekday, so this week's dates would be a claim about when they air,
    // and there is no "today" in a season that is over (owner, 2026-09-17).
    mockApi({
      'GET /api/schedule?year=2026&season=SUMMER': {
        body: { ...SCHEDULE_PAGE, year: 2026, season: 'SUMMER' },
      },
    })

    renderSchedule('/schedule?year=2026&season=SUMMER')
    await screen.findByText('Summer 2026')

    // Monday-first, because a browse has no today to open on.
    expect(daysShown()).toEqual(['Monday', 'Tuesday', 'Wednesday'])
    expect(screen.queryByText('Today')).not.toBeInTheDocument()
    expect(document.body.querySelectorAll('[data-today="true"]')).toHaveLength(0)

    // The arrows still walk the same seven days.
    expect(previousDay()).toBeDisabled()
    await userEvent.click(nextDay())
    expect(daysShown()).toEqual(['Tuesday', 'Wednesday', 'Thursday'])
  })

  it('dates the bar again as soon as the live season is back on screen', async () => {
    mockApi({
      'GET /api/schedule?year=2026&season=SUMMER': {
        body: {
          ...SCHEDULE_PAGE,
          year: 2026,
          season: 'SUMMER',
          next: { year: 2026, season: 'FALL' },
        },
      },
      'GET /api/schedule?year=2026&season=FALL': { body: SCHEDULE_PAGE },
    })

    renderSchedule('/schedule?year=2026&season=SUMMER')
    await screen.findByText('Summer 2026')
    expect(daysShown()).toEqual(['Monday', 'Tuesday', 'Wednesday'])

    // Fall 2026 is the season the clock is in, however it was arrived at: the
    // page compares the grid's season with the live one, not the URL.
    await userEvent.click(screen.getByRole('button', { name: 'Next season' }))

    expect(await screen.findByText('Fall 2026')).toBeInTheDocument()
    await waitFor(() => {
      expect(daysShown()).toEqual(['Tue 6 OctToday', 'Wed 7 Oct', 'Thu 8 Oct'])
    })
  })

  it('opens on the viewer’s day when their zone is already on the next one', async () => {
    // 23:00 UTC on that Tuesday is Wednesday morning in Tokyo.
    vi.setSystemTime(new Date('2026-10-06T23:00:00Z'))
    mockApi({
      'GET /api/schedule': { body: { ...SCHEDULE_PAGE, timezone: 'Asia/Tokyo' } },
    })

    renderSchedule()
    await screen.findByText('Fall 2026')

    expect(daysShown()).toEqual(['Wed 7 OctToday', 'Thu 8 Oct', 'Fri 9 Oct'])
    expect(dayColumn('Wednesday')).toHaveAttribute('data-today', 'true')
    expect(screen.queryByRole('region', { name: 'Tuesday' })).not.toBeInTheDocument()
  })

  it('marks followed shows and leaves the rest plain', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    const followed = document.body.querySelectorAll('[data-following="true"]')
    expect(followed).toHaveLength(1)
    // A quiet ember left rule over a 7% tint — "a little highlight" (owner,
    // 2026-09-17), not a badge.
    expect(followed[0]?.className).toContain('border-l-2')
    expect(followed[0]?.className).toContain('var(--arc-ember)')
    expect(followed[0]?.textContent).toContain(FRIEREN.title.preferred)

    // And it says so in words as well as in colour. The text sits outside the
    // link, whose accessible name is the title alone.
    const note = within(dayColumn('Tuesday')).getByText('On your list')
    expect(note).toHaveClass('sr-only')
    expect(note.closest('a')).toBeNull()

    // The Apothecary is not followed, so it gets no rule and no note.
    const thursdayRow = within(dayColumn('Thursday')).getByRole('listitem')
    expect(thursdayRow).not.toHaveAttribute('data-following')
    expect(within(dayColumn('Thursday')).queryByText('On your list')).not.toBeInTheDocument()
  })

  it('carries the “via MAL” caveat as a tooltip', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    // The caveat still travels with the row (FR-C6), but a bordered warning
    // pill was the loudest thing on the page for the smallest piece of
    // information on it. It is the link's `title` now, and nothing at all on
    // an AniList row.
    await userEvent.click(
      screen.getByRole('button', { name: 'Show Movies, OVAs and unscheduled (1)' }),
    )
    expect(screen.queryByText('via MAL')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: FRIEREN_SPECIAL.title.preferred })).toHaveAttribute(
      'title',
      'AniList is unavailable; this row came from MyAnimeList',
    )
    expect(
      within(dayColumn('Tuesday')).getByRole('link', { name: FRIEREN.title.preferred }),
    ).not.toHaveAttribute('title')
  })

  it('keeps the row readable: no status control in the grid, 56px thumbs', async () => {
    mockApi({ 'GET /api/schedule': { body: SCHEDULE_PAGE } })

    renderSchedule()
    await screen.findByText('Fall 2026')

    // The whole row is the link, named by the title alone — the slot inside it
    // is detail, not part of what the link is called.
    const row = within(dayColumn('Tuesday')).getByRole('listitem')
    const link = within(row).getByRole('link', { name: FRIEREN.title.preferred })
    expect(link).toHaveAttribute('href', `/anime/${String(FRIEREN.id)}`)
    expect(link.querySelector('.w-\\[56px\\]')).not.toBeNull()
    expect(link.textContent).toContain('18:30 · Ep 5')

    // The control is out of the day columns from `lg` up; it is one tap away
    // on the show page, and it stays put on the stacked phone layout.
    const control = within(row).getByRole('combobox')
    expect(control.closest('.lg\\:hidden')).not.toBeNull()
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

  it('offers a retry that asks for the season again', async () => {
    const fetchMock = mockApi({
      'GET /api/schedule': { status: 500, body: { detail: 'boom' } },
    })

    renderSchedule()
    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === 'GET /api/schedule')).toHaveLength(2)
    })
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

  it('moves the window on with the clock past midnight', () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    // 23:59 in Europe/Berlin, the fixture's zone.
    vi.setSystemTime(new Date('2026-10-06T21:59:00Z'))

    renderSeeded()
    expect(daysShown()).toEqual(['Tue 6 OctToday', 'Wed 7 Oct', 'Thu 8 Oct'])

    act(() => {
      vi.setSystemTime(new Date('2026-10-06T22:01:00Z'))
      vi.advanceTimersByTime(60_000)
    })

    // A tab left open overnight opens on the new today, dates and all.
    expect(daysShown()).toEqual(['Wed 7 OctToday', 'Thu 8 Oct', 'Fri 9 Oct'])
    expect(dayColumn('Wednesday')).toHaveAttribute('data-today', 'true')
  })

  it('stops the interval when the page goes away', () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(TUESDAY)

    renderSeeded()
    expect(vi.getTimerCount()).toBe(1)

    cleanup()
    expect(vi.getTimerCount()).toBe(0)
  })
})
