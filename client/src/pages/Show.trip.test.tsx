import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AnimeDetail, EpisodeOut } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import type { Trip, TripEpisode } from '@/lib/trips'
import { setDownloads } from '@/offline/downloads'
import { Show } from '@/pages/Show'
import { FRIEREN, FRIEREN_DETAIL, PLAY_INFO } from '@/test/animeFixtures'
import {
  callTo,
  jsonBodyOf,
  mockApi,
  requestsMade,
  TEST_DEMO_USER,
  TEST_USER,
  type MockRoutes,
} from '@/test/apiMock'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

/**
 * "Prepare for a trip" on the show page (M19 T6, FR-A12): the control, its
 * bounds and refusals; the active-trip panel; and how the episode rows read a
 * trip-only episode — the viewer's own, and somebody else's.
 */

const ME = { body: TEST_USER }
const DETAIL = `GET /api/anime/${String(FRIEREN.id)}`
const TRIP_PATH = `/api/anime/${String(FRIEREN.id)}/trip`
const TRIP_ID = 77

function tripEpisode(
  id: number,
  number: number,
  phase: TripEpisode['phase'],
  patch: Partial<TripEpisode> = {},
): TripEpisode {
  return { episode_id: id, number, phase, progress: null, size: null, delivered: false, ...patch }
}

function trip(episodes: TripEpisode[], patch: Partial<Trip> = {}): Trip {
  return {
    id: TRIP_ID,
    anime_id: FRIEREN.id,
    anime_title: FRIEREN.title.preferred,
    first_number: episodes[0]?.number ?? 1,
    last_number: episodes[episodes.length - 1]?.number ?? 1,
    count: episodes.length,
    state: 'active',
    created_at: '2026-10-06T08:00:00Z',
    deadline_at: '2026-10-20T08:00:00Z',
    episodes,
    ...patch,
  }
}

function renderShow() {
  const router = createMemoryRouter(
    [
      { path: '/anime/:id', element: <Show /> },
      { path: '/watch/:episodeId', element: <p>player</p> },
      { path: '/downloads', element: <p>downloads page</p> },
    ],
    { initialEntries: [`/anime/${String(FRIEREN.id)}`] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

/** A browser that can keep a trip: OPFS, a Worker, and a `<video>` that plays H.264. */
function pretendCapable() {
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: { getDirectory: () => Promise.reject(new Error('not in tests')) },
  })
  vi.stubGlobal('Worker', class {})
  vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
}

function installManager() {
  const harness = managerHarness()
  harness.manager.setOwner(TEST_USER.id)
  setDownloads(harness.manager)
  return harness
}

/** The routes every test needs, added to `routes` itself so a test may change them later. */
function api(routes: MockRoutes) {
  routes['GET /api/auth/me'] ??= ME
  routes['GET /api/trips/current'] ??= { body: null }
  return mockApi(routes)
}

beforeEach(() => {
  pretendCapable()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  Reflect.deleteProperty(navigator, 'storage')
  setDownloads(null)
})

describe('Prepare for a trip', () => {
  it('stops the stepper at the server’s cap', async () => {
    installManager()
    api({ [DETAIL]: { body: { ...FRIEREN_DETAIL, trip_limits: { max_episodes: 3 } } } })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))

    expect(screen.getByRole('spinbutton', { name: 'Episodes' })).toHaveAttribute('max', '3')
    expect(screen.getByTestId('trip-range')).toHaveTextContent('Episodes 2–5')
  })

  it('steps between 1 and the aired episodes after progress, with the range and an estimate', async () => {
    installManager()
    api({ [DETAIL]: { body: FRIEREN_DETAIL } })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))

    // Episode 1 is watched and 3 has not aired: 2, 4, 5, 6, 7 — five at most.
    const card = screen.getByRole('form', { name: 'Prepare for a trip' })
    const count = within(card).getByRole('spinbutton', { name: 'Episodes' })
    expect(count).toHaveValue(5)
    expect(count).toHaveAttribute('max', '5')
    expect(within(card).getByRole('button', { name: 'More episodes' })).toBeDisabled()
    expect(within(card).getByTestId('trip-range')).toHaveTextContent('Episodes 2–7')
    expect(within(card).getByText(/About 500 MB \(estimate\)/)).toBeInTheDocument()

    const fewer = within(card).getByRole('button', { name: 'Fewer episodes' })
    for (let i = 0; i < 6; i += 1) await user.click(fewer)
    expect(count).toHaveValue(1)
    expect(fewer).toBeDisabled()
    expect(within(card).getByTestId('trip-range')).toHaveTextContent('Episode 2')
    expect(within(card).getByRole('button', { name: 'Prepare 1 episode' })).toBeEnabled()
  })

  /** What the browser reports for Arc's storage (owner, 2026-10-07). */
  function browserReports(usage: number, quota: number) {
    Object.defineProperty(navigator, 'storage', {
      configurable: true,
      value: {
        getDirectory: () => Promise.reject(new Error('not in tests')),
        estimate: () => Promise.resolve({ usage, quota }),
      },
    })
  }

  it('caps the stepper at the room the browser reports, and says it is an estimate', async () => {
    installManager()
    // 340 MB free: about 3 episodes at 110 MB each.
    browserReports(4_700_000_000, 5_040_000_000)
    api({ [DETAIL]: { body: FRIEREN_DETAIL } })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))

    const card = screen.getByRole('form', { name: 'Prepare for a trip' })
    expect(await within(card).findByTestId('trip-room')).toHaveTextContent(
      /The browser reports room for about 3 episodes on this device \(324 MB free of 4\.7 GB\)\. It is an estimate/,
    )
    const count = within(card).getByRole('spinbutton', { name: 'Episodes' })
    expect(count).toHaveAttribute('max', '3')
    expect(count).toHaveValue(3)
    expect(within(card).getByText(/the browser reports 324 MB free/)).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Prepare 3 episodes' })).toBeEnabled()
  })

  it('offers no trip when the browser reports no room for one episode', async () => {
    installManager()
    browserReports(5_000_000_000, 5_050_000_000)
    api({ [DETAIL]: { body: FRIEREN_DETAIL } })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))

    const card = screen.getByRole('form', { name: 'Prepare for a trip' })
    expect(await within(card).findByTestId('trip-room')).toHaveTextContent(
      /no room for another episode/,
    )
    expect(within(card).getByRole('button', { name: 'Prepare 1 episode' })).toBeDisabled()
  })

  it('leaves the stepper alone when the browser reports room enough', async () => {
    installManager()
    browserReports(4_700_000_000, 32_400_000_000)
    api({ [DETAIL]: { body: FRIEREN_DETAIL } })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))

    const card = screen.getByRole('form', { name: 'Prepare for a trip' })
    expect(await within(card).findByText(/the browser reports .* free/)).toBeInTheDocument()
    expect(within(card).queryByTestId('trip-room')).not.toBeInTheDocument()
    expect(within(card).getByRole('spinbutton', { name: 'Episodes' })).toHaveAttribute('max', '5')
  })

  it('asks for the chosen count and shows the trip it made', async () => {
    installManager()
    const made = trip([tripEpisode(9002, 2, 'preparing'), tripEpisode(9004, 4, 'searching')])
    const routes: MockRoutes = {
      [DETAIL]: { body: FRIEREN_DETAIL },
      [`POST ${TRIP_PATH}`]: { status: 201, body: made },
    }
    const fetchMock = api(routes)
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))
    await user.click(screen.getByRole('button', { name: 'Fewer episodes' }))
    await user.click(screen.getByRole('button', { name: 'Fewer episodes' }))
    await user.click(screen.getByRole('button', { name: 'Fewer episodes' }))
    // What the refetch after the request answers: the show, with its trip.
    routes[DETAIL] = { body: { ...FRIEREN_DETAIL, trip: made } }
    await user.click(screen.getByRole('button', { name: 'Prepare 2 episodes' }))

    expect(jsonBodyOf(callTo(fetchMock, TRIP_PATH))).toEqual({ count: 2 })
    expect(await screen.findByRole('heading', { name: 'Trip · Episodes 2–4' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Prepare for a trip' })).not.toBeInTheDocument()
  })

  it.each([
    [409, 'storage_held', /short of disk space/],
    [422, 'nothing_aired', /Nothing has aired after where you are/],
    [422, 'count_out_of_range', /more episodes than this server allows/],
  ])('says a %i %s in a sentence', async (status, detail, sentence) => {
    installManager()
    api({
      [DETAIL]: { body: FRIEREN_DETAIL },
      [`POST ${TRIP_PATH}`]: { status, body: { detail } },
    })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))
    await user.click(screen.getByRole('button', { name: /^Prepare \d+ episodes?$/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(sentence)
  })

  it('links to the show that already holds the one trip', async () => {
    installManager()
    const elsewhere = trip([tripEpisode(5, 1, 'searching')], {
      anime_id: 4242,
      anime_title: 'Kusuriya no Hitorigoto',
    })
    api({
      [DETAIL]: { body: FRIEREN_DETAIL },
      'GET /api/trips/current': { body: elsewhere },
    })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Prepare for a trip' }))

    const link = await screen.findByRole('link', { name: 'Kusuriya no Hitorigoto' })
    expect(link).toHaveAttribute('href', '/anime/4242')
    expect(screen.getByRole('button', { name: /^Prepare \d+ episodes$/ })).toBeDisabled()
  })

  it('is not offered to the demo account', async () => {
    installManager()
    api({ 'GET /api/auth/me': { body: TEST_DEMO_USER }, [DETAIL]: { body: FRIEREN_DETAIL } })

    renderShow()

    await screen.findByRole('heading', { level: 1 })
    expect(screen.queryByRole('button', { name: 'Prepare for a trip' })).not.toBeInTheDocument()
  })

  it('says why where the device cannot play the copies', async () => {
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('')
    installManager()
    api({ [DETAIL]: { body: FRIEREN_DETAIL } })

    renderShow()

    expect(
      await screen.findByText(
        'This device can’t play Arc’s small copies, so it can’t prepare a trip.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Prepare for a trip' })).not.toBeInTheDocument()
  })
})

describe('the active trip', () => {
  /** Episodes 2, 4, 5, 6, 7 in five phases; 7 is on this device already. */
  const ACTIVE = trip([
    tripEpisode(9002, 2, 'downloading', { progress: 0.42 }),
    tripEpisode(9004, 4, 'preparing', { progress: 0.3 }),
    tripEpisode(9005, 5, 'expired'),
    tripEpisode(9006, 6, 'waiting_space'),
    tripEpisode(9007, 7, 'delivered', { delivered: true }),
  ])

  function withTrip(t: Trip = ACTIVE): AnimeDetail {
    return {
      ...FRIEREN_DETAIL,
      trip: t,
      episodes: FRIEREN_DETAIL.episodes.map((episode) =>
        t.episodes.some((row) => row.episode_id === episode.id)
          ? { ...episode, state: 'not_wanted', trip_only: true }
          : episode,
      ),
    }
  }

  it('lists each episode where it stands', async () => {
    const { manager } = installManager()
    void manager
    api({ [DETAIL]: { body: withTrip() } })

    renderShow()

    const panel = (await screen.findByRole('heading', { name: 'Trip · Episodes 2–7' })).closest(
      'section',
    ) as HTMLElement
    const lines = within(panel)
      .getAllByRole('listitem')
      .map((item) => item.textContent)
    expect(lines).toEqual([
      'Episode 2Downloading to the server 42%',
      'Episode 4Making the small copy 30%',
      'Episode 5Expired on the serverAsk again',
      'Episode 6Waiting for disk space',
      'Episode 7Not on this deviceAsk again',
    ])
    expect(within(panel).getByText(/0 of 5 on this device/)).toBeInTheDocument()
    expect(within(panel).getByText(/downloads run while Arc is open/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Prepare for a trip' })).not.toBeInTheDocument()
  })

  it('says plainly when a trip download is paused because the device is full', async () => {
    const record = downloadedRecord(TEST_USER.id, PLAY_INFO, 98_000_000)
    const harness = managerHarness({
      initial: [
        recordEntry({
          ...record,
          episodeId: 9002,
          name: 'episode-9002-o.mp4',
          variant: 'small',
          tripId: TRIP_ID,
          state: 'paused',
          reason: 'quota',
          bytes: 8_000_000,
        }),
      ],
    })
    harness.files.set('episode-9002-o.mp4', 8_000_000)
    harness.manager.setOwner(TEST_USER.id)
    await harness.manager.hydrate()
    setDownloads(harness.manager)
    api({ [DETAIL]: { body: withTrip() } })

    renderShow()

    const panel = (await screen.findByRole('heading', { name: 'Trip · Episodes 2–7' })).closest(
      'section',
    ) as HTMLElement
    const notice = within(panel).getByRole('alert')
    expect(notice).toHaveTextContent(/out of space — free some room and the download continues/)
    expect(within(notice).getByRole('link', { name: 'Go to Downloads' })).toHaveAttribute(
      'href',
      '/downloads',
    )
    expect(within(panel).getByText(/Paused · this device is out of space/)).toBeInTheDocument()
  })

  it('reads the device’s own copy first', async () => {
    const record = { ...downloadedRecord(TEST_USER.id, PLAY_INFO, 98_000_000) }
    const harness = managerHarness({
      initial: [recordEntry({ ...record, episodeId: 9007, tripId: TRIP_ID, confirm: 'done' })],
    })
    harness.files.set(record.name, record.bytes)
    harness.manager.setOwner(TEST_USER.id)
    await harness.manager.hydrate()
    setDownloads(harness.manager)
    api({ [DETAIL]: { body: withTrip() } })

    renderShow()

    expect(await screen.findByText('On this device · 93.5 MB')).toBeInTheDocument()
    expect(screen.getByText(/1 of 5 on this device/)).toBeInTheDocument()
  })

  it('cancels the trip after an in-page confirmation', async () => {
    installManager()
    const confirmSpy = vi.spyOn(window, 'confirm')
    const fetchMock = api({
      [DETAIL]: { body: withTrip() },
      [`DELETE /api/trips/${String(TRIP_ID)}`]: { status: 204 },
    })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Cancel trip' }))
    const ask = screen.getByRole('group', { name: 'Cancel this trip?' })
    expect(ask).toHaveTextContent('Episodes already on this device stay.')
    await user.click(within(ask).getByRole('button', { name: 'Cancel trip' }))

    expect(requestsMade(fetchMock)).toContain(`DELETE /api/trips/${String(TRIP_ID)}`)
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('asks again for an expired episode', async () => {
    installManager()
    const fetchMock = api({
      [DETAIL]: { body: withTrip() },
      [`POST /api/trips/${String(TRIP_ID)}/episodes/9005/again`]: { body: ACTIVE },
    })
    const user = userEvent.setup()

    renderShow()
    await user.click(await screen.findByRole('button', { name: 'Ask again for episode 5' }))

    expect(requestsMade(fetchMock)).toContain(
      `POST /api/trips/${String(TRIP_ID)}/episodes/9005/again`,
    )
  })

  it('drives the row’s keep-offline button by the phase', async () => {
    installManager()
    const phases = trip([
      tripEpisode(9002, 2, 'downloading', { progress: 0.4 }),
      tripEpisode(9004, 4, 'preparing', { progress: 0.3 }),
      tripEpisode(9005, 5, 'available', {
        progress: 1,
        size: 98_000_000,
        url: '/media/9005/offline.mp4',
      }),
      tripEpisode(9006, 6, 'available', { progress: 1, size: 98_000_000, url: null }),
    ])
    api({ [DETAIL]: { body: withTrip(phases) } })

    renderShow()

    expect(
      await screen.findByRole('button', {
        name: 'Episode 2 is on its way to the server for your trip',
      }),
    ).toHaveAttribute('aria-disabled', 'true')
    expect(
      screen.getByRole('button', {
        name: 'Episode 4 is being prepared on the server for your trip, 30%',
      }),
    ).toHaveAttribute('data-state', 'preparing')
    expect(screen.getByRole('button', { name: 'Keep episode 5 offline' })).toBeInTheDocument()
    // Available, but no URL from the server yet: still the server's.
    expect(
      screen.getByRole('button', { name: 'Episode 6 is on its way to the server for your trip' }),
    ).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('Trip · Downloading to the server 40%')).toBeInTheDocument()
  })
})

describe('somebody else’s trip-only episode', () => {
  it('reads “Not prepared for streaming”, with nothing to play or keep', async () => {
    installManager()
    const tripOnly: AnimeDetail = {
      ...FRIEREN_DETAIL,
      episodes: FRIEREN_DETAIL.episodes.map((episode): EpisodeOut =>
        episode.id === 9004 ? { ...episode, state: 'not_wanted', trip_only: true } : episode,
      ),
    }
    api({ [DETAIL]: { body: tripOnly } })

    renderShow()

    expect(await screen.findByText('Not prepared for streaming')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^4\. / })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /episode 4/i })).not.toBeInTheDocument()
  })
})

describe('a kept trip episode that later becomes ready (owner, 2026-10-06)', () => {
  it('turns into an ordinary ready row, and the device copy still counts', async () => {
    const record = {
      ...downloadedRecord(TEST_USER.id, PLAY_INFO, 98_000_000),
      name: 'episode-9001-o.mp4',
      variant: 'small' as const,
      tripId: TRIP_ID,
      confirm: 'done' as const,
    }
    const harness = managerHarness({ initial: [recordEntry(record)] })
    harness.files.set(record.name, record.bytes)
    harness.manager.setOwner(TEST_USER.id)
    await harness.manager.hydrate()
    setDownloads(harness.manager)
    const kept = trip([
      tripEpisode(9001, 1, 'delivered', { delivered: true, url: '/media/9001/offline.mp4' }),
    ])
    // Episode 1 is `ready` in the fixture: the look-ahead prepared it for streaming.
    api({ [DETAIL]: { body: { ...FRIEREN_DETAIL, trip: kept } } })

    renderShow()

    expect(await screen.findByRole('link', { name: /^1\. / })).toHaveAttribute(
      'href',
      '/watch/9001',
    )
    expect(screen.getByRole('button', { name: 'Episode 1 is on this device' })).toBeInTheDocument()
    // The row says what any ready row says; only the panel's heading names the trip.
    expect(screen.queryByText(/^Trip · (?!Episode)/)).not.toBeInTheDocument()
    expect(screen.getByText('Ready to play')).toBeInTheDocument()
    expect(screen.queryByText('Not prepared for streaming')).not.toBeInTheDocument()
  })
})
