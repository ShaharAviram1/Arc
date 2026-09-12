import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AnimeDetail, EpisodeOut } from '@/lib/anime'
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
  FRIEREN_DETAIL_VIA_OFFLINE,
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
const OFFLINE_NOTICE =
  'Catalogue data from the weekly offline import — live sources are unavailable. Air dates unknown until a live source answers.'
const DETAIL_PATH = `GET /api/anime/${FRIEREN.id}`
const LIST_PATH = `/api/list/${FRIEREN.id}`
/** The one failed episode in the fixture, the only one with a Retry button. */
const TRANSCODE_PATH = '/api/episodes/9007/transcode'
/** Episode 1 is the fixture's only watched episode; episode 2 has aired and is not. */
const WATCHED_PATH = '/api/episodes/9001/watched'
const UNWATCHED_PATH = '/api/episodes/9002/watched'
const PROGRESS_RESULT = { completed: true, newly_completed: true, list_progress: 2 }

/** The design's meta line: studio, when it aired, how much of it there is. */
const META_LINE = 'Madhouse · Fall 2023 · 28 episodes · 1 watched'

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

/** The hero, by the one `<h1>` on the page: the frame is its own section. */
function hero(): HTMLElement {
  return screen.getByRole('heading', { level: 1 }).closest('section') as HTMLElement
}

/** The fixture with one episode replaced, keyed by its position in the list. */
function withEpisode(index: number, patch: Partial<EpisodeOut>): AnimeDetail {
  return {
    ...FRIEREN_DETAIL,
    episodes: FRIEREN_DETAIL.episodes.map((episode, at) =>
      at === index ? { ...episode, ...patch } : episode,
    ),
  }
}

/**
 * A fixed mid-day instant, for the one thing on this page that asks what day
 * it is. Mid-day in Berlin (TEST_USER's zone) so that "an hour from now" and
 * "this evening" are still the same day there — a clock read off the machine
 * made the "Airs tonight" test fail after 23:00 local.
 */
const MIDDAY = new Date('2026-09-12T10:00:00Z')

/** Freezes `Date` only: `waitFor` and `userEvent` keep their real timers. */
function freezeClock(): void {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(MIDDAY)
}

afterEach(() => {
  vi.useRealTimers()
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
    expect(screen.getByText(META_LINE)).toBeInTheDocument()
    expect(screen.getByText('Fantasy')).toBeInTheDocument()

    // One row per episode, numbered, with the untitled one falling back to its
    // number rather than leaving the line blank.
    expect(screen.getByText('1. The Journey’s End')).toBeInTheDocument()
    expect(screen.getByText('2. Episode 2')).toBeInTheDocument()
    // Each row says one thing about its state, in the state column.
    expect(screen.getByText('Watched')).toBeInTheDocument()
    expect(screen.getByText('Preparing 30%')).toBeInTheDocument()
    expect(screen.getByText('Searching')).toBeInTheDocument()
  })

  it('fills the hero with the banner when the catalogue has one', async () => {
    const banner = 'https://example.test/frieren-banner.jpg'
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: { ...FRIEREN_DETAIL, banner_url: banner } satisfies AnimeDetail },
    })

    renderShow()

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    const frame = hero()

    // The shape is measured off-frame first: a banner narrow enough to fill
    // the hero does, a 4.75:1 AniList strip is washed instead.
    const probe = frame.querySelector(`img[src="${banner}"]`) as HTMLImageElement
    Object.defineProperty(probe, 'naturalWidth', { value: 1920, configurable: true })
    Object.defineProperty(probe, 'naturalHeight', { value: 1080, configurable: true })
    fireEvent.load(probe)

    expect(frame.querySelector('img')).toHaveAttribute('src', banner)
    expect(frame.querySelector('img')).toHaveAttribute('loading', 'eager')
    // Nothing has to be blurred to stand in for a banner.
    expect(frame.querySelector('[data-hero-backdrop]')).toBeNull()

    // The frame is the same fixed 21:9 box the show page always draws.
    const box = (frame.querySelector('img') as HTMLImageElement).parentElement as HTMLElement
    expect(box).toHaveClass('aspect-[21/9]')
    expect(box.style.aspectRatio).toBe('')
  })

  it('washes the poster when the only banner is an AniList strip', async () => {
    const banner = 'https://example.test/frieren-banner.jpg'
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: { ...FRIEREN_DETAIL, banner_url: banner } satisfies AnimeDetail },
    })

    renderShow()

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    const frame = hero()
    const probe = frame.querySelector(`img[src="${banner}"]`) as HTMLImageElement
    Object.defineProperty(probe, 'naturalWidth', { value: 1900, configurable: true })
    Object.defineProperty(probe, 'naturalHeight', { value: 400, configurable: true })
    fireEvent.load(probe)

    // Cropping 1900×400 to 21:9 shows the middle half of it, so the frame
    // takes the poster treatment instead — at the same 21:9.
    expect(frame.querySelector(`img[src="${banner}"]`)).toBeNull()
    const backdrop = frame.querySelector('[data-hero-backdrop]')
    expect(backdrop).toHaveAttribute('src', FRIEREN_DETAIL.cover_large_url)
    expect(backdrop?.parentElement).toHaveClass('aspect-[21/9]')
    expect((backdrop?.parentElement as HTMLElement).style.aspectRatio).toBe('')
  })

  it('never stretches a poster across the hero when the show has no banner', async () => {
    const poster = 'https://example.test/no-banner-large.jpg'
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: {
          ...FRIEREN_DETAIL,
          banner_url: null,
          cover_url: 'https://example.test/no-banner.jpg',
          cover_large_url: poster,
        } satisfies AnimeDetail,
      },
    })

    renderShow()

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    const frame = hero()

    // The cover, blurred and darkened, is the ground — not a picture to read.
    const backdrop = frame.querySelector('[data-hero-backdrop]')
    expect(backdrop).toHaveAttribute('src', poster)
    expect(backdrop).toHaveAttribute('aria-hidden', 'true')
    expect(backdrop).toHaveClass('blur-[40px]')

    // The one crisp copy keeps its own 2:3 ratio, beside the title.
    const key = frame.querySelector('[data-hero-poster]') as HTMLElement
    expect(key.querySelector('img')).toHaveAttribute('src', poster)
    expect(key.firstElementChild).toHaveClass('aspect-[2/3]')
    expect(key).not.toContainElement(screen.getByRole('heading', { level: 1 }))

    // The frame itself stays 21:9: the wash fills it, it does not shape it.
    expect(backdrop?.parentElement).toHaveClass('aspect-[21/9]')
    expect((backdrop?.parentElement as HTMLElement).style.aspectRatio).toBe('')
  })

  it('offers the first ready episode as the primary action', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const play = await screen.findByRole('link', { name: 'Play episode 1' })
    expect(play).toHaveAttribute('href', '/watch/9001')
  })

  it('offers nothing to play when no episode has arrived', async () => {
    const nothingReady: AnimeDetail = {
      ...FRIEREN_DETAIL,
      episodes: FRIEREN_DETAIL.episodes.map((episode) =>
        episode.state === 'ready' ? { ...episode, state: 'preparing' } : episode,
      ),
    }
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: nothingReady } })

    renderShow()

    expect(await screen.findByRole('button', { name: 'Nothing ready yet' })).toBeDisabled()
    expect(screen.queryByRole('link', { name: /^Play episode/ })).not.toBeInTheDocument()
  })

  it('makes only a ready episode row a link into the player', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const row = await screen.findByRole('link', { name: '1. The Journey’s End' })
    expect(row).toHaveAttribute('href', '/watch/9001')
    // Every other episode is text: there is nothing to open.
    expect(screen.queryByRole('link', { name: '2. Episode 2' })).not.toBeInTheDocument()
  })

  it('says when an upcoming episode lands today, in the viewer’s own zone', async () => {
    freezeClock()
    // 20:00 in Berlin on the frozen day: still to come, still today.
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: withEpisode(2, { air_at: '2026-09-12T18:00:00Z', aired: false }),
      },
    })

    renderShow()

    expect(await screen.findByText('Airs tonight')).toBeInTheDocument()
  })

  it('says only that an episode further out has not aired', async () => {
    freezeClock()
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: withEpisode(2, { air_at: '2026-09-13T18:00:00Z', aired: false }),
      },
    })

    renderShow()

    expect(await screen.findByText('Not yet aired')).toBeInTheDocument()
    expect(screen.queryByText('Airs tonight')).not.toBeInTheDocument()
  })

  it('names an episode that has not aired yet, whatever Arc wants of it', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    // Episode 3 is `not_wanted` and unaired: the row says the fact, not the
    // internal state, and never in an error colour.
    expect(await screen.findByText('Not yet aired')).toBeInTheDocument()
  })

  it('calls an episode nobody asked for "Not fetched", not "Not wanted"', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: withEpisode(2, { aired: true, air_at: '2023-10-13T14:00:00Z' }),
      },
    })

    renderShow()

    // A resting state is a fact about Arc's queue, not a refusal, and never
    // reads in an error colour.
    expect(await screen.findByText('Not fetched')).toBeInTheDocument()
    expect(screen.queryByText('Not wanted')).not.toBeInTheDocument()
  })

  it('carries the air date, the runtime, the resolution and the group in the row', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    // Episode 1 is ready, so its own rendition says how long it runs and what
    // it is; episode 4 has only the release Arc picked for it (FR-A3).
    expect(await screen.findByText(/24 min · 1080p$/)).toBeInTheDocument()
    expect(screen.getByText(/1080p · SubsPlease$/)).toBeInTheDocument()
  })

  it('drops the group from the row when the parser did not find one', async () => {
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

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    expect(screen.queryByText(/SubsPlease/)).not.toBeInTheDocument()
  })

  it('prefers what the transcode produced over what the release claimed', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: withEpisode(0, { release: { ...CHOSEN_RELEASE, resolution: '720p' } }),
      },
    })

    renderShow()

    expect(await screen.findByText(/24 min · 1080p · SubsPlease$/)).toBeInTheDocument()
    expect(screen.queryByText(/720p/)).not.toBeInTheDocument()
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

  it('numbers the franchise, with this show in its place and side stories outside it', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const shelf = (await screen.findByRole('heading', { name: 'The franchise, in order' })).closest(
      'section',
    )
    if (shelf === null) throw new Error('the franchise shelf has no section')

    // No prequel in the fixture: this show is first, the sequel follows it,
    // and the side story carries a dash rather than a number it has not earned.
    const orders = within(shelf)
      .getAllByText(/^(\d+|—)$/)
      .map((node) => node.textContent)
    expect(orders).toEqual(['1', '2', '—'])
    expect(within(shelf).getByText('TV · 28 episodes · 2023 · This show')).toBeInTheDocument()
    // The side story is cached, so its card carries the counts; the sequel is
    // not, so the relation itself is the most useful thing left to say.
    expect(within(shelf).getByText('SPECIAL · 4 episodes · 2024')).toBeInTheDocument()
    expect(within(shelf).getByText('TV · Sequel')).toBeInTheDocument()
  })

  it('draws a cached relation’s own key visual, and a placeholder for the rest', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const shelf = (await screen.findByRole('heading', { name: 'The franchise, in order' })).closest(
      'section',
    )
    if (shelf === null) throw new Error('the franchise shelf has no section')

    // The large key visual wins over the small one; the uncached sequel has
    // no image at all, so its card keeps the stripes.
    const sources = [...shelf.querySelectorAll('img')].map((image) => image.getAttribute('src'))
    expect(sources).toEqual([FRIEREN.cover_large_url, LINKED_RELATION.cover_large_url])
  })

  it('links a relation by the id the server sends for it', async () => {
    // `anime_id` is what the card follows; `id` may be absent on the wire.
    const relation = { ...LINKED_RELATION, id: null, anime_id: 4242 }
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: { ...FRIEREN_DETAIL, relations: [relation] } },
    })

    renderShow()

    expect(
      await screen.findByRole('link', { name: LINKED_RELATION.title.preferred }),
    ).toHaveAttribute('href', '/anime/4242')
  })

  it('says nothing about the franchise when the show has no relations', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: { ...FRIEREN_DETAIL, relations: [] } },
    })

    renderShow()

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    expect(
      screen.queryByRole('heading', { name: 'The franchise, in order' }),
    ).not.toBeInTheDocument()
  })

  it('credits the studio as an auteur, and the staff the catalogue published', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: {
        body: {
          ...FRIEREN_DETAIL,
          credits: [
            { role: 'Studio', name: 'Madhouse' },
            { role: 'Director', name: 'Keiichirou Saitou' },
            { role: 'Music', name: 'Evan Call' },
          ],
        },
      },
    })

    renderShow()

    const made = (await screen.findByRole('heading', { name: 'Made by' })).closest('section')
    if (made === null) throw new Error('the credits have no section')
    expect(within(made).getByText('Director')).toBeInTheDocument()
    expect(within(made).getByText('Keiichirou Saitou')).toBeInTheDocument()
    expect(within(made).getByText('Evan Call')).toBeInTheDocument()
  })

  it('falls back to the studio alone when the catalogue published no staff', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const made = (await screen.findByRole('heading', { name: 'Made by' })).closest('section')
    if (made === null) throw new Error('the credits have no section')
    expect(within(made).getByText('Studio')).toBeInTheDocument()
    expect(within(made).getByText('Madhouse')).toBeInTheDocument()
  })

  it('hides the credits entirely when there is not even a studio', async () => {
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: { ...FRIEREN_DETAIL, studio: null } },
    })

    renderShow()

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    expect(screen.queryByRole('heading', { name: 'Made by' })).not.toBeInTheDocument()
  })

  it('renders a show with no romaji title', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL_NO_STATUS } })

    renderShow()

    expect(
      await screen.findByRole('heading', { name: FRIEREN.title.preferred }),
    ).toBeInTheDocument()
    expect(screen.getByText('葬送のフリーレン')).toBeInTheDocument()
    // The airing status is not part of the design's meta line, so a null one
    // is not rendered as "Null" either.
    expect(screen.getByText(META_LINE)).toBeInTheDocument()
  })

  it('shows how far a downloading episode has got, as a bar and as text (FR-A7)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const bar = await screen.findByRole('progressbar', { name: 'Acquisition progress' })
    expect(bar).toHaveAttribute('aria-valuenow', '42')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(bar).toHaveAttribute('aria-valuetext', '42%')
    expect(screen.getByText('Downloading 42%')).toBeInTheDocument()
    // Only work in flight gets a bar: searching and wanted are just a word.
    expect(screen.getAllByRole('progressbar')).toHaveLength(2)
  })

  it('shows how far a transcode has got, on the same bar (FR-P4)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    // Named for the job it is measuring, since the words beside it are not
    // read out with it.
    const bar = await screen.findByRole('progressbar', { name: 'Preparing' })
    expect(bar).toHaveAttribute('aria-valuenow', '30')
    expect(bar).toHaveAttribute('aria-valuetext', '30%')
    expect(screen.getByText('Preparing 30%')).toBeInTheDocument()
  })

  it('says why a transcode failed, and offers no retry to a non-admin (FR-P4)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    const hint = await screen.findByLabelText(`Failed: ${FAILURE_REASON}`)
    expect(hint).toHaveAttribute('title', FAILURE_REASON)
    expect(screen.getByText('Failed')).toBeInTheDocument()
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
    mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: withEpisode(0, { state: 'matching', download_progress: null }) },
    })

    renderShow()

    await screen.findByRole('heading', { name: FRIEREN.title.preferred })
    const bars = screen.getAllByRole('progressbar')
    expect(bars.map((bar) => bar.getAttribute('aria-valuenow'))).toEqual(['100', '30', '42'])
    expect(screen.getByText('Matching 100%')).toBeInTheDocument()
  })

  it('says why an unavailable episode is not coming (FR-A6)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL } })

    renderShow()

    // Hoverable for a mouse, and spelled out for a screen reader, which cannot.
    const hint = await screen.findByLabelText(`Unavailable: ${UNAVAILABLE_REASON}`)
    expect(hint).toHaveAttribute('title', UNAVAILABLE_REASON)
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
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

  it('PUTs the chosen score', async () => {
    const fetchMock = mockApi({
      'GET /api/auth/me': ME,
      [DETAIL_PATH]: { body: FRIEREN_DETAIL_ON_LIST },
      [`PUT ${LIST_PATH}`]: { body: listEntry({ progress: 4, score: 7 }) },
    })

    renderShow()
    await screen.findByText('Watched 4 / 28')
    await userEvent.setup().selectOptions(screen.getByLabelText('Score'), '7')

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`PUT ${LIST_PATH}`)
    })
    expect(jsonBodyOf(callTo(fetchMock, LIST_PATH))).toEqual({ score: 7 })
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
    expect(screen.queryByText(OFFLINE_NOTICE)).not.toBeInTheDocument()
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

  it('flags a record the weekly offline import filled (FR-C6)', async () => {
    mockApi({ 'GET /api/auth/me': ME, [DETAIL_PATH]: { body: FRIEREN_DETAIL_VIA_OFFLINE } })

    renderShow()

    expect(await screen.findByText(OFFLINE_NOTICE)).toBeInTheDocument()
    // The offline catalogue carries no air dates, so nothing is estimated
    // either — and the MAL caveat is not what this record has.
    expect(screen.queryByText(MAL_NOTICE)).not.toBeInTheDocument()
    expect(screen.queryAllByText('est.')).toHaveLength(0)
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

    it('shows the state for a watched episode and takes the mark off again', async () => {
      const fetchMock = mockApi({
        'GET /api/auth/me': ME,
        [DETAIL_PATH]: { body: FRIEREN_DETAIL },
        [`DELETE ${WATCHED_PATH}`]: {
          body: { completed: false, newly_completed: false, list_progress: null },
        },
      })

      renderShow()

      expect(await screen.findByText('Watched')).toBeInTheDocument()
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
