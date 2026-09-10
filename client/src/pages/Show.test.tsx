import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import type { MalSync } from '@/lib/mal'
import { Show } from '@/pages/Show'
import {
  CHOSEN_RELEASE,
  FAILURE_REASON,
  FRIEREN,
  FRIEREN_DETAIL,
  FRIEREN_DETAIL_NO_STATUS,
  FRIEREN_DETAIL_ON_LIST,
  FRIEREN_DETAIL_VIA_MAL,
  FRIEREN_SPECIAL,
  LINKED_RELATION,
  listEntry,
  MAL_WRITE_ERROR,
  UNAVAILABLE_REASON,
  UNLINKED_RELATION,
} from '@/test/animeFixtures'
import { callTo, jsonBodyOf, mockApi, requestsMade, TEST_ADMIN, TEST_USER } from '@/test/apiMock'

const ME = { body: TEST_USER }
const MAL_NOTICE =
  'Catalogue data via MyAnimeList — AniList is unavailable. Air dates are estimated.'
const DETAIL_PATH = `GET /api/anime/${FRIEREN.id}`
const LIST_PATH = `/api/list/${FRIEREN.id}`
/** The one failed episode in the fixture, the only one with a Retry button. */
const TRANSCODE_PATH = '/api/episodes/9007/transcode'
/** Episode 1 is the fixture's only watched episode; episode 2 has aired and is not. */
const WATCHED_PATH = '/api/episodes/9001/watched'
const UNWATCHED_PATH = '/api/episodes/9002/watched'
const PROGRESS_RESULT = { completed: true, newly_completed: true, list_progress: 2 }

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

    const bar = await screen.findByRole('progressbar', { name: 'Acquisition progress' })
    expect(bar).toHaveAttribute('aria-valuenow', '42')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(bar).toHaveAttribute('aria-valuetext', '42%')
    expect(screen.getByText('42%')).toBeInTheDocument()
    // Only work in flight gets a bar: searching and wanted are just badges.
    expect(screen.getAllByRole('progressbar')).toHaveLength(2)
    expect(screen.getAllByText('Searching')).toHaveLength(2)
  })

  it('shows how far a transcode has got, on the same bar (FR-P4)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    // Named for the job it is measuring, since the badge beside it is not read
    // out with it.
    const bar = await screen.findByRole('progressbar', { name: 'Preparing' })
    expect(bar).toHaveAttribute('aria-valuenow', '30')
    expect(bar).toHaveAttribute('aria-valuetext', '30%')
    expect(screen.getByText('30%')).toBeInTheDocument()
  })

  it('says why a transcode failed, and offers no retry to a non-admin (FR-P4)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const hint = await screen.findByLabelText(`Failed: ${FAILURE_REASON}`)
    expect(hint).toHaveAttribute('title', FAILURE_REASON)
    expect(screen.getAllByText('Failed')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('lets an admin put a failed episode back through the transcoder (FR-P4)', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_ADMIN },
      [DETAIL_PATH]: { body: FRIEREN_DETAIL },
      [`POST ${TRANSCODE_PATH}`]: { status: 202 },
    })

    renderShow()
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`POST ${TRANSCODE_PATH}`)
    })
    // One button, on the one failed episode.
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
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
    expect(bars.map((bar) => bar.getAttribute('aria-valuenow'))).toEqual(['100', '30', '42'])
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

    // Episode 1 is ready and carries no release, so the only thing under its
    // title is what the transcode produced — the exact-text match lands on the
    // cell itself, not on an inner span.
    const title = await screen.findByText('The Journey’s End')
    expect(title.tagName).toBe('TD')
    expect(screen.getAllByText('[SubsPlease] · 1080p · 123 seeders')).toHaveLength(1)
  })

  it('says what a ready episode actually is, under its title (FR-P4)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    expect(await screen.findByText('1080p · subs en · audio ja')).toBeInTheDocument()
    // Only the ready episode has a rendition; nothing else grew a second line.
    expect(screen.getAllByText(/subs en/)).toHaveLength(1)
  })

  it('appends the rendition to the release line when the episode has both', async () => {
    const [first, ...rest] = FRIEREN_DETAIL.episodes
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: { ...FRIEREN_DETAIL, episodes: [{ ...first, release: CHOSEN_RELEASE }, ...rest] },
      },
    })

    renderShow()

    expect(
      await screen.findByText('[SubsPlease] · 1080p · 123 seeders · 1080p · subs en · audio ja'),
    ).toBeInTheDocument()
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

  it('offers a retry that asks for the show again', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { status: 500, body: { detail: 'boom' } },
    })

    renderShow()
    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === DETAIL_PATH)).toHaveLength(2)
    })
  })

  describe('marking an episode watched (FR-W3)', () => {
    it('offers a mark for every aired episode that is not watched yet', async () => {
      mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

      renderShow()

      // Six of the seven episodes have aired; the watched one shows Unmark.
      const marks = await screen.findAllByRole('button', { name: 'Mark watched' })
      expect(marks).toHaveLength(5)
      // The unaired episode gets neither control: there is nothing to have watched.
      expect(screen.getAllByRole('button', { name: 'Unmark' })).toHaveLength(1)
    })

    it('posts the mark and refreshes the show', async () => {
      const fetchMock = mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: { body: FRIEREN_DETAIL },
        [`POST ${UNWATCHED_PATH}`]: { body: PROGRESS_RESULT },
      })

      renderShow()

      const marks = await screen.findAllByRole('button', { name: 'Mark watched' })
      await userEvent.click(marks[0] as HTMLElement)

      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain(`POST ${UNWATCHED_PATH}`)
      })
    })

    it('shows the marker for a watched episode and takes it off again', async () => {
      const fetchMock = mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: { body: FRIEREN_DETAIL },
        [`DELETE ${WATCHED_PATH}`]: {
          body: { completed: false, newly_completed: false, list_progress: null },
        },
      })

      renderShow()

      expect(await screen.findByLabelText('Watched')).toBeInTheDocument()
      await userEvent.click(screen.getByRole('button', { name: 'Unmark' }))

      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain(`DELETE ${WATCHED_PATH}`)
      })
    })

    it('says so when the write fails', async () => {
      mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: { body: FRIEREN_DETAIL },
        [`POST ${UNWATCHED_PATH}`]: { status: 500, body: { detail: 'boom' } },
      })

      renderShow()

      const marks = await screen.findAllByRole('button', { name: 'Mark watched' })
      await userEvent.click(marks[0] as HTMLElement)

      expect(await screen.findByRole('alert')).toHaveTextContent('Could not save that.')
    })
  })

  describe('the MAL sync indicator (FR-M6)', () => {
    function detailWithSync(sync: MalSync) {
      return {
        ...FRIEREN_DETAIL_ON_LIST,
        list_entry: { ...listEntry({ progress: 4, score: 9 }), mal_sync: sync },
      }
    }

    it('reads as synced when the last write landed', async () => {
      mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: {
          body: detailWithSync({
            state: 'synced',
            error: null,
            last_write_at: '2026-09-07T08:00:00Z',
          }),
        },
      })

      renderShow()

      expect(await screen.findByText('MAL: synced')).toBeInTheDocument()
    })

    it('reads as pending while a write is queued', async () => {
      mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: {
          body: detailWithSync({ state: 'pending', error: null, last_write_at: null }),
        },
      })

      renderShow()

      expect(await screen.findByText('MAL: pending')).toBeInTheDocument()
    })

    it('names the failure and links to the sync log', async () => {
      mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: {
          body: detailWithSync({
            state: 'failed',
            error: MAL_WRITE_ERROR,
            last_write_at: '2026-09-07T08:00:00Z',
          }),
        },
      })

      renderShow()

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent(`MAL: failed — ${MAL_WRITE_ERROR}`)
      expect(within(alert).getByRole('link', { name: 'Sync log' })).toHaveAttribute('href', '/mal')
    })

    it('says nothing at all when the account is not linked', async () => {
      mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: {
          body: detailWithSync({ state: 'unlinked', error: null, last_write_at: null }),
        },
      })

      renderShow()

      expect(await screen.findByText('Watched 4 / 28')).toBeInTheDocument()
      expect(screen.queryByText(/^MAL:/)).not.toBeInTheDocument()
    })

    it('says nothing when the server sent no sync state at all', async () => {
      mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL_ON_LIST } })

      renderShow()

      expect(await screen.findByText('Watched 4 / 28')).toBeInTheDocument()
      expect(screen.queryByText(/^MAL:/)).not.toBeInTheDocument()
    })
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
