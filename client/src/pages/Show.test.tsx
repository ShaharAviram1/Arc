import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Show } from '@/pages/Show'
import {
  FRIEREN,
  FRIEREN_DETAIL,
  FRIEREN_DETAIL_NO_STATUS,
  FRIEREN_DETAIL_ON_LIST,
  FRIEREN_DETAIL_VIA_MAL,
  FRIEREN_SPECIAL,
  LINKED_RELATION,
  listEntry,
  UNAVAILABLE_REASON,
  UNLINKED_RELATION,
} from '@/test/animeFixtures'
import { callTo, jsonBodyOf, mockApi, requestsMade, TEST_USER } from '@/test/apiMock'

const ME = { body: TEST_USER }
const MAL_NOTICE =
  'Catalogue data via MyAnimeList — AniList is unavailable. Air dates are estimated.'
const DETAIL_PATH = `GET /api/anime/${FRIEREN.id}`
const LIST_PATH = `/api/list/${FRIEREN.id}`

function renderShow(id: number | string = FRIEREN.id) {
  const router = createMemoryRouter(
    [
      { path: '/anime/:id', element: <Show /> },
      { path: '/watch/:episodeId', element: <p>player</p> },
      { path: '/search', element: <p>search page</p> },
    ],
    { initialEntries: [`/anime/${id}`] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Show', () => {
  it('renders the title, meta line and every episode', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    expect(
      await screen.findByRole('heading', { name: FRIEREN.title.preferred }),
    ).toBeInTheDocument()
    expect(screen.getByText('Sousou no Frieren · 葬送のフリーレン')).toBeInTheDocument()
    expect(
      screen.getByText('TV · 28 episodes · Finished · Fall 2023 · Madhouse'),
    ).toBeInTheDocument()
    expect(screen.getByText('Fantasy')).toBeInTheDocument()

    // One row per episode, with the untitled one falling back to its number.
    expect(screen.getByText('The Journey’s End')).toBeInTheDocument()
    expect(screen.getByText('Episode 2')).toBeInTheDocument()
    // Each unplayable episode shows its state twice: badge, and in place of Play.
    expect(screen.getAllByText('Preparing')).toHaveLength(2)
    expect(screen.getAllByText('Not wanted')).toHaveLength(2)
    expect(screen.getByLabelText('Watched')).toBeInTheDocument()
  })

  it('links a relation Arc has a row for, to that row', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    expect(
      await screen.findByRole('link', { name: LINKED_RELATION.title.preferred }),
    ).toHaveAttribute('href', `/anime/${FRIEREN_SPECIAL.id}`)
  })

  it('renders a relation Arc has no row for as plain text, with a reason', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const title = UNLINKED_RELATION.title.preferred
    expect(await screen.findByText(title)).toHaveAttribute('title', 'Not in the catalogue yet')
    // No page to link to, and no link out to AniList/MAL either.
    expect(screen.queryByRole('link', { name: title })).not.toBeInTheDocument()
  })

  it('renders a show with no airing status and no romaji title', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL_NO_STATUS } })

    renderShow()

    expect(
      await screen.findByRole('heading', { name: FRIEREN.title.preferred }),
    ).toBeInTheDocument()
    // The status is dropped from the meta line rather than rendered as "Null".
    expect(screen.getByText('TV · 28 episodes · Fall 2023 · Madhouse')).toBeInTheDocument()
    expect(screen.getByText('葬送のフリーレン')).toBeInTheDocument()
  })

  it('offers Play only for episodes that are ready', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const play = await screen.findAllByRole('link', { name: 'Play' })
    expect(play).toHaveLength(1)
    expect(play[0]).toHaveAttribute('href', '/watch/9001')
  })

  it('shows how far a downloading episode has got, as a bar and as text (FR-A7)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const bar = await screen.findByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '42')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(bar).toHaveAttribute('aria-valuetext', '42%')
    expect(screen.getByText('42%')).toBeInTheDocument()
    // Only the transfer gets a bar: searching and wanted are just badges.
    expect(screen.getAllByRole('progressbar')).toHaveLength(1)
    expect(screen.getAllByText('Searching')).toHaveLength(2)
  })

  it('reads downloaded and matching as complete', async () => {
    const [first, ...rest] = FRIEREN_DETAIL.episodes
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: {
          ...FRIEREN_DETAIL,
          episodes: [{ ...first, state: 'matching', download_progress: null }, ...rest],
        },
      },
    })

    renderShow()

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    const bars = screen.getAllByRole('progressbar')
    expect(bars.map((bar) => bar.getAttribute('aria-valuenow'))).toEqual(['100', '42'])
    expect(screen.getByText('100%')).toBeInTheDocument()
  })

  it('says why an unavailable episode is not coming (FR-A6)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    // Hoverable for a mouse, and spelled out for a screen reader, which cannot.
    const hint = await screen.findByLabelText(`Unavailable: ${UNAVAILABLE_REASON}`)
    expect(hint).toHaveAttribute('title', UNAVAILABLE_REASON)
    expect(screen.getAllByText('Unavailable')).toHaveLength(2)
  })

  it('names the chosen release under the episode title (FR-A3)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    expect(await screen.findByText('[SubsPlease] · 1080p · 123 seeders')).toBeInTheDocument()
  })

  it('drops the group from the release line when the parser did not find one', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: {
          ...FRIEREN_DETAIL,
          episodes: FRIEREN_DETAIL.episodes.map((episode) =>
            episode.release === null
              ? episode
              : { ...episode, release: { ...episode.release, group: null } },
          ),
        },
      },
    })

    renderShow()

    expect(await screen.findByText('1080p · 123 seeders')).toBeInTheDocument()
  })

  it('shows no release line for an episode nothing has been picked for', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    // Episode 1 is ready and carries no release, so its cell is the title alone
    // — the exact-text match lands on the cell itself, not on an inner span.
    const title = await screen.findByText('The Journey’s End')
    expect(title.tagName).toBe('TD')
    expect(screen.getAllByText('[SubsPlease] · 1080p · 123 seeders')).toHaveLength(1)
  })

  it('shows score and progress only once the show is on the list', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL_ON_LIST } })

    renderShow()

    expect(await screen.findByText('Watched 4 / 28')).toBeInTheDocument()
    expect(screen.getByLabelText('Score')).toHaveValue('9')
  })

  it('PUTs the chosen status', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: FRIEREN_DETAIL },
      [`PUT ${LIST_PATH}`]: { body: listEntry({ status: 'watching' }) },
    })

    renderShow()
    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    await userEvent.setup().selectOptions(screen.getByLabelText('List status'), 'watching')

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`PUT ${LIST_PATH}`)
    })
    expect(jsonBodyOf(callTo(fetchMock, LIST_PATH))).toEqual({ status: 'watching' })
  })

  it('DELETEs the entry when the show is taken off the list', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: FRIEREN_DETAIL_ON_LIST },
      [`DELETE ${LIST_PATH}`]: { status: 204 },
    })

    renderShow()
    await screen.findByText('Watched 4 / 28')
    await userEvent.setup().selectOptions(screen.getByLabelText('List status'), '')

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`DELETE ${LIST_PATH}`)
    })
  })

  it('says nothing about the catalogue source when AniList filled the record', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()
    await screen.findByRole('heading', { name: FRIEREN.title.preferred })

    expect(screen.queryByText(MAL_NOTICE)).not.toBeInTheDocument()
    expect(screen.queryAllByText('est.')).toHaveLength(0)
  })

  it('flags MAL-sourced data and every estimated air date (FR-C6)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL_VIA_MAL } })

    renderShow()

    expect(await screen.findByText(MAL_NOTICE)).toBeInTheDocument()
    const estimated = screen.getAllByText('est.')
    expect(estimated).toHaveLength(FRIEREN_DETAIL_VIA_MAL.episodes.length)
    expect(estimated[0]).toHaveAttribute('title', 'Estimated from the broadcast slot')
  })

  it('links out to both catalogues when it has both ids', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const anilist = await screen.findByRole('link', { name: 'AniList' })
    expect(anilist).toHaveAttribute('href', 'https://anilist.co/anime/154587')
    expect(anilist).toHaveAttribute('target', '_blank')
    expect(anilist).toHaveAttribute('rel', 'noreferrer')
    expect(screen.getByRole('link', { name: 'MAL' })).toHaveAttribute(
      'href',
      'https://myanimelist.net/anime/52991',
    )
  })

  it('omits the AniList link while the show has no AniList id', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL_VIA_MAL } })

    renderShow()
    await screen.findByRole('heading', { name: FRIEREN.title.preferred })

    expect(screen.queryByRole('link', { name: 'AniList' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'MAL' })).toBeInTheDocument()
  })

  it('names the outage when both catalogue sources are down', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { status: 502, body: { detail: 'catalogue is unavailable' } },
    })

    renderShow()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The catalogue is unavailable right now. Try again in a few minutes.',
    )
  })

  it('shows the not-found state for an unknown id', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      'GET /api/anime/999999': { status: 404, body: { detail: 'anime not found' } },
    })

    renderShow(999999)

    expect(await screen.findByRole('heading', { name: 'Show not found' })).toBeInTheDocument()
  })

  it('shows the not-found state for a non-numeric id without calling the API', () => {
    const fetchMock = mockApi({ 'GET /api/auth/me': ME })

    renderShow('nope')

    expect(screen.getByRole('heading', { name: 'Show not found' })).toBeInTheDocument()
    expect(requestsMade(fetchMock)).not.toContain('GET /api/anime/NaN')
  })
})
