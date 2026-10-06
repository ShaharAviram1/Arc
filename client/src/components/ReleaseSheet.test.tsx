import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChoosePackButton, EpisodeMoreMenu } from '@/components/ReleaseSheet'
import type { AnimeDetail, EpisodeOut } from '@/lib/anime'
import { createQueryClient } from '@/lib/queryClient'
import { coversLabel, numberRuns, type Releases } from '@/lib/releases'
import { Show } from '@/pages/Show'
import { FRIEREN, FRIEREN_DETAIL } from '@/test/animeFixtures'
import {
  callTo,
  jsonBodyOf,
  mockApi,
  requestsMade,
  TEST_DEMO_USER,
  TEST_USER,
  type MockRoutes,
} from '@/test/apiMock'

/**
 * "Change release…" and "Choose pack…" (FR-A13): the sheet lists what Arc
 * found and why it would not take some of it, a pasted link to the wrong host
 * is refused in a sentence, a choice calls the route and refreshes the show,
 * and the demo account sees none of it.
 */

const EPISODE_ID = 9004
const LIST = `/api/episodes/${String(EPISODE_ID)}/releases`
const CHOOSE = `/api/episodes/${String(EPISODE_ID)}/release`
const DETAIL = `/api/anime/${String(FRIEREN.id)}`

const RELEASES: Releases = {
  scope: 'episode',
  scope_id: EPISODE_ID,
  number: 4,
  cached: false,
  searched_seconds_ago: 0,
  current: {
    title: '[Dead] Sousou no Frieren - 04 [1080p]',
    kind: 'single',
    state: 'stalledDL',
    progress: 0.02,
    manual: false,
  },
  candidates: [
    {
      id: 'aaaa1111',
      title: '[SubsPlease] Sousou no Frieren - 04 (1080p)',
      group: 'SubsPlease',
      resolution: '1080p',
      size: '1.4 GiB',
      seeders: 155,
      leechers: 3,
      kind: 'single',
      trusted: true,
      covers: [4],
      acceptable: true,
      reason: null,
    },
    {
      id: 'bbbb2222',
      title: '[Erai-raws] Sousou no Frieren - 01 ~ 12 [1080p][BATCH]',
      group: 'Erai-raws',
      resolution: '1080p',
      size: '16.2 GiB',
      seeders: 40,
      leechers: 9,
      kind: 'batch',
      trusted: false,
      covers: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
      acceptable: true,
      reason: null,
    },
    {
      id: 'cccc3333',
      title: '[SubsPlease] Sousou no Frieren S2 - 04 (1080p)',
      group: 'SubsPlease',
      resolution: '1080p',
      size: '1.4 GiB',
      seeders: 700,
      leechers: 30,
      kind: 'single',
      trusted: true,
      covers: [4],
      acceptable: false,
      reason: 'season 2, not 1',
    },
    {
      id: 'dddd4444',
      title: '[Dead] Sousou no Frieren - 04 [1080p]',
      group: 'Dead',
      resolution: '1080p',
      size: '1.3 GiB',
      seeders: 0,
      leechers: 0,
      kind: 'single',
      trusted: false,
      covers: [4],
      acceptable: false,
      reason: 'this is the release downloading now',
      current: true,
    },
  ],
}

function renderMenu(routes: MockRoutes) {
  const fetchMock = mockApi(routes)
  render(
    <QueryClientProvider client={createQueryClient()}>
      <EpisodeMoreMenu animeId={FRIEREN.id} episodeId={EPISODE_ID} number={4} />
    </QueryClientProvider>,
  )
  return fetchMock
}

async function openSheet() {
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'More for episode 4' }))
  await user.click(screen.getByRole('menuitem', { name: 'Change release…' }))
  return user
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the release sheet', () => {
  it('lists what is downloading, the candidates and why Arc would not take some', async () => {
    renderMenu({ [`GET ${LIST}`]: { body: RELEASES } })
    await openSheet()

    const sheet = screen.getByRole('dialog', { name: 'Change release · Episode 4' })
    expect(await within(sheet).findByText('Downloading now', { selector: 'p' })).toBeInTheDocument()
    expect(within(sheet).getByText('Single · 2%')).toBeInTheDocument()

    const list = within(sheet).getByRole('list', { name: 'Releases on Nyaa' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(4)
    expect(rows[0]).toHaveTextContent('[SubsPlease] Sousou no Frieren - 04 (1080p)')
    expect(rows[0]).toHaveTextContent('Single · 1080p · 1.4 GiB · 155 seeders · episode 4')
    expect(rows[1]).toHaveTextContent('Pack · 1080p · 16.2 GiB · 40 seeders · covers 1–12')
    expect(rows[2]).toHaveTextContent('Arc would not take it: season 2, not 1')
    expect(within(rows[3] as HTMLElement).getByRole('radio')).toBeDisabled()
    expect(rows[3]).toHaveTextContent('Downloading now')

    expect(within(sheet).getByRole('button', { name: 'Use this release' })).toBeDisabled()
  })

  it('uses the chosen candidate, then refreshes the show and closes', async () => {
    const fetchMock = renderMenu({
      [`GET ${LIST}`]: { body: RELEASES },
      [`POST ${CHOOSE}`]: {
        status: 202,
        body: {
          title: RELEASES.candidates[1]?.title,
          kind: 'batch',
          info_hash: 'b'.repeat(40),
          episodes: [{ episode_id: EPISODE_ID, number: 4, state: 'downloading' }],
        },
      },
    })
    const user = await openSheet()
    const sheet = screen.getByRole('dialog')
    await user.click(await within(sheet).findByRole('radio', { name: /01 ~ 12/ }))
    await user.click(within(sheet).getByRole('button', { name: 'Use this release' }))

    expect(jsonBodyOf(callTo(fetchMock, CHOOSE))).toEqual({ candidate_id: 'bbbb2222' })
    expect(await screen.findByRole('button', { name: 'More for episode 4' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('says a pasted link to another host in a sentence', async () => {
    const sentence = 'Arc only takes links to Nyaa (nyaa.si), or a magnet link.'
    const fetchMock = renderMenu({
      [`GET ${LIST}`]: { body: RELEASES },
      [`POST ${CHOOSE}`]: {
        status: 422,
        body: { detail: { code: 'host_not_allowed', message: sentence } },
      },
    })
    const user = await openSheet()
    const sheet = screen.getByRole('dialog')
    await within(sheet).findAllByRole('radio')
    await user.type(
      within(sheet).getByLabelText('Or paste a magnet link or a Nyaa link'),
      'https://evil.example/download/1.torrent',
    )
    await user.click(within(sheet).getByRole('button', { name: 'Use this release' }))

    expect(await within(sheet).findByRole('alert')).toHaveTextContent(sentence)
    expect(jsonBodyOf(callTo(fetchMock, CHOOSE))).toEqual({
      link: 'https://evil.example/download/1.torrent',
    })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('says a refused pack in the server’s words', async () => {
    renderMenu({
      [`GET ${LIST}`]: { body: RELEASES },
      [`POST ${CHOOSE}`]: {
        status: 422,
        body: {
          detail: { code: 'pack_selects_nothing', message: 'No file in that pack is episode 4.' },
        },
      },
    })
    const user = await openSheet()
    const sheet = screen.getByRole('dialog')
    await user.click(await within(sheet).findByRole('radio', { name: /01 ~ 12/ }))
    await user.click(within(sheet).getByRole('button', { name: 'Use this release' }))
    expect(await within(sheet).findByRole('alert')).toHaveTextContent(
      'No file in that pack is episode 4.',
    )
  })

  it('closes on Escape', async () => {
    renderMenu({ [`GET ${LIST}`]: { body: RELEASES } })
    const user = await openSheet()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('scopes the trip’s sheet to the trip', async () => {
    const tripReleases: Releases = { ...RELEASES, scope: 'trip', scope_id: 77, current: null }
    const fetchMock = mockApi({ 'GET /api/trips/77/releases': { body: tripReleases } })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <ChoosePackButton tripId={77} animeId={FRIEREN.id} />
      </QueryClientProvider>,
    )
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Choose pack…' }))

    const sheet = screen.getByRole('dialog', { name: 'Choose a pack for this trip' })
    expect(await within(sheet).findByText('Nothing is downloading yet.')).toBeInTheDocument()
    expect(requestsMade(fetchMock)).toContain('GET /api/trips/77/releases')
  })
})

describe('the episode row', () => {
  function detailWith(patch: Partial<EpisodeOut>): AnimeDetail {
    return {
      ...FRIEREN_DETAIL,
      episodes: FRIEREN_DETAIL.episodes.map((episode) =>
        episode.id === EPISODE_ID ? { ...episode, ...patch } : episode,
      ),
    }
  }

  function renderShow(routes: MockRoutes) {
    const fetchMock = mockApi({ 'GET /api/trips/current': { body: null }, ...routes })
    const router = createMemoryRouter([{ path: '/anime/:id', element: <Show /> }], {
      initialEntries: [`/anime/${String(FRIEREN.id)}`],
    })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    return fetchMock
  }

  it('refreshes the row after a choice', async () => {
    const fetchMock = renderShow({
      'GET /api/auth/me': { body: TEST_USER },
      [`GET ${DETAIL}`]: { body: detailWith({ wanted_by_me: true }) },
      [`GET ${LIST}`]: { body: RELEASES },
      [`POST ${CHOOSE}`]: {
        status: 202,
        body: {
          title: 'x',
          kind: 'single',
          info_hash: 'a'.repeat(40),
          episodes: [{ episode_id: EPISODE_ID, number: 4, state: 'downloading' }],
        },
      },
    })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'More for episode 4' }))
    await user.click(screen.getByRole('menuitem', { name: 'Change release…' }))
    await user.click(await screen.findByRole('radio', { name: /Frieren - 04 \(1080p\)/ }))
    await user.click(screen.getByRole('button', { name: 'Use this release' }))

    await vi.waitFor(() => {
      expect(requestsMade(fetchMock).filter((call) => call === `GET ${DETAIL}`).length).toBe(2)
    })
    expect(jsonBodyOf(callTo(fetchMock, CHOOSE))).toEqual({ candidate_id: 'aaaa1111' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('offers "Change release…" for an episode the viewer wants', async () => {
    renderShow({
      'GET /api/auth/me': { body: TEST_USER },
      [`GET ${DETAIL}`]: { body: detailWith({ wanted_by_me: true }) },
    })
    expect(await screen.findByRole('button', { name: 'More for episode 4' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'More for episode 3' })).not.toBeInTheDocument()
  })

  it('offers nothing for an episode the viewer does not want', async () => {
    renderShow({
      'GET /api/auth/me': { body: TEST_USER },
      [`GET ${DETAIL}`]: { body: detailWith({ wanted_by_me: false }) },
    })
    await screen.findByText('4. The Land Where Souls Rest')
    expect(screen.queryByRole('button', { name: /^More for episode/ })).not.toBeInTheDocument()
  })

  it('offers nothing to the demo account', async () => {
    renderShow({
      'GET /api/auth/me': { body: TEST_DEMO_USER },
      [`GET ${DETAIL}`]: { body: detailWith({ wanted_by_me: true }) },
    })
    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    expect(screen.queryByRole('button', { name: /^More for episode/ })).not.toBeInTheDocument()
  })
})

describe('the labels', () => {
  it('collapses runs of numbers', () => {
    expect(numberRuns([3, 1, 2, 5, 7, 8])).toBe('1–3, 5, 7–8')
    expect(numberRuns([])).toBe('')
  })

  it('says what a candidate covers', () => {
    expect(coversLabel({ kind: 'single', covers: [7] })).toBe('episode 7')
    expect(coversLabel({ kind: 'batch', covers: [1, 2, 3] })).toBe('covers 1–3')
    expect(coversLabel({ kind: 'batch', covers: null })).toBe('contents read when chosen')
  })
})
