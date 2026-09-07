import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Home } from '@/pages/Home'
import {
  APOTHECARY,
  CONTINUE_FRIEREN,
  EMPTY_HOME,
  FRIEREN,
  HOME_PAGE,
  HOME_PAGE_CONTINUE,
  HOME_PAGE_CONTINUE_NO_DURATION,
  HOME_PAGE_DOWNLOADING,
  HOME_PAGE_PREPARING,
} from '@/test/animeFixtures'
import { mockApi, TEST_USER, type MockRoutes } from '@/test/apiMock'

const HEALTH = { status: 'ok', version: '0.1.0', env: 'dev' }

function renderHome(routes: MockRoutes) {
  const fetchMock = mockApi({
    'GET /api/auth/me': { body: TEST_USER },
    'GET /api/health': { body: HEALTH },
    ...routes,
  })
  const router = createMemoryRouter(
    [
      { path: '/', element: <Home /> },
      { path: '/anime/:id', element: <p>show page</p> },
      { path: '/watch/:episodeId', element: <p>player</p> },
    ],
    { initialEntries: ['/'] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return fetchMock
}

function section(name: string): HTMLElement {
  return screen.getByRole('heading', { level: 2, name }).parentElement as HTMLElement
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Home', () => {
  it('shows how far behind the viewer is on each followed show', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE } })

    expect(await screen.findByText('Behind by 4 · 7 of 12 aired')).toBeInTheDocument()

    const behind = within(section('Behind on'))
    expect(behind.getByRole('link', { name: FRIEREN.title.preferred })).toHaveAttribute(
      'href',
      `/anime/${FRIEREN.id}`,
    )
    // The card carries the same status control as everywhere else (FR-W2).
    expect(behind.getByRole('combobox')).toHaveValue('watching')
    expect(behind.getByText(/^Latest episode /)).toBeInTheDocument()
  })

  it('lists this week’s episodes with their state, marking estimated dates', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE } })
    await screen.findByText('Behind by 4 · 7 of 12 aired')

    const week = within(section('New this week'))
    expect(week.getAllByRole('listitem')).toHaveLength(2)
    expect(week.getByText('Episode 7')).toBeInTheDocument()
    expect(week.getByText('Episode 2')).toBeInTheDocument()
    expect(week.getByText('Ready')).toBeInTheDocument()
    expect(week.getByText('Preparing')).toBeInTheDocument()
    expect(week.getByRole('link', { name: APOTHECARY.title.preferred })).toBeInTheDocument()

    // Exactly one of the two came back with a synthesised air date (FR-C6).
    expect(week.getAllByText('est.')).toHaveLength(1)
  })

  it('puts the download percentage after the state badge (FR-A7)', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE_DOWNLOADING } })

    expect(await screen.findByText('Downloading')).toBeInTheDocument()
    const week = within(section('New this week'))
    expect(week.getByText('42%')).toBeInTheDocument()
  })

  it('puts the transcode percentage there too (FR-P4)', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE_PREPARING } })

    expect(await screen.findByText('Preparing')).toBeInTheDocument()
    const week = within(section('New this week'))
    expect(week.getByText('30%')).toBeInTheDocument()
  })

  it('shows no percentage for an episode that is not being fetched', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE } })
    await screen.findByText('Behind by 4 · 7 of 12 aired')

    expect(within(section('New this week')).queryByText(/%$/)).not.toBeInTheDocument()
  })

  it('offers each part-watched episode with how far in it is (FR-W1)', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE_CONTINUE } })

    expect(await screen.findByText('12:34 / 23:56')).toBeInTheDocument()

    const strip = within(section('Continue watching'))
    // Both the cover and the episode line go straight to the player.
    expect(strip.getByRole('link', { name: 'Episode 5' })).toHaveAttribute(
      'href',
      `/watch/${CONTINUE_FRIEREN.episode.id}`,
    )
    expect(
      strip.getByRole('link', { name: `Resume ${FRIEREN.title.preferred} Episode 5` }),
    ).toHaveAttribute('href', `/watch/${CONTINUE_FRIEREN.episode.id}`)
    // The title still goes to the show page, as it does in every other section.
    expect(strip.getByRole('link', { name: FRIEREN.title.preferred })).toHaveAttribute(
      'href',
      `/anime/${FRIEREN.id}`,
    )

    const bar = strip.getByRole('progressbar', { name: 'Progress through Episode 5' })
    expect(bar).toHaveAttribute('aria-valuenow', '53')
    expect(bar).toHaveAttribute('aria-valuetext', '12:34 / 23:56')
  })

  it('shows the position alone when the episode has no known duration', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE_CONTINUE_NO_DURATION } })

    expect(await screen.findByText('12:34')).toBeInTheDocument()

    const strip = within(section('Continue watching'))
    // No "/ 0:00" tail, and no percentage invented from a length nobody knows.
    expect(strip.queryByText(/\//)).not.toBeInTheDocument()

    const bar = strip.getByRole('progressbar', { name: 'Progress through Episode 5' })
    expect(bar).not.toHaveAttribute('aria-valuenow')
    expect(bar).toHaveAttribute('aria-valuetext', '12:34')
  })

  it('says so plainly when nothing is in progress', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE } })

    expect(
      await screen.findByText('Nothing in progress — start an episode and it will show up here.'),
    ).toBeInTheDocument()
  })

  it('has an empty line for each section when there is nothing to show', async () => {
    renderHome({ 'GET /api/home': { body: EMPTY_HOME } })

    expect(
      await screen.findByText('Nothing to catch up on — every followed show is up to date.'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Nothing in progress — start an episode and it will show up here.'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('No episodes aired in the last seven days for the shows you follow.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('reports a failure to build the page', async () => {
    renderHome({ 'GET /api/home': { status: 500, body: { detail: 'boom' } } })

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load your home page.')
  })

  it('still shows the API status when /api/health responds', async () => {
    renderHome({ 'GET /api/home': { body: EMPTY_HOME } })

    expect(await screen.findByText('API: ok')).toBeInTheDocument()
    expect(screen.getByText('0.1.0 · dev')).toBeInTheDocument()
  })

  it('shows "API: unreachable" when the API cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const router = createMemoryRouter([{ path: '/', element: <Home /> }], {
      initialEntries: ['/'],
    })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    // The shared query defaults retry once with a ~1s backoff, so allow for that.
    expect(await screen.findByText('API: unreachable', {}, { timeout: 5000 })).toBeInTheDocument()
  })
})
