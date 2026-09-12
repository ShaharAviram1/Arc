import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import type { AnimeSummary, EpisodeOut, MyListItem } from '@/lib/anime'
import type { RecsPage } from '@/lib/recs'
import { weekdayInTimezone, WEEKDAY_LABELS, type SchedulePage } from '@/lib/schedule'
import { Home } from '@/pages/Home'
import {
  APOTHECARY,
  CONTINUE_FRIEREN,
  EMPTY_HOME,
  EMPTY_SCHEDULE,
  FRIEREN,
  HOME_PAGE,
  HOME_PAGE_CONTINUE,
  HOME_PAGE_CONTINUE_NO_DURATION,
  listEntry,
  RECS_PAGE,
  RECS_PAGE_EMPTY,
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

const HEALTH = { status: 'ok', version: '0.1.0', env: 'dev', tmdb_enabled: false }

/** Every shelf on the page asks for something; a test opts into the answers. */
function renderHome(routes: MockRoutes) {
  const fetchMock = mockApi({
    'GET /api/auth/me': { body: TEST_USER },
    'GET /api/health': { body: HEALTH },
    'GET /api/schedule': { body: EMPTY_SCHEDULE },
    'GET /api/recs': { body: RECS_PAGE_EMPTY },
    'GET /api/list': { body: [] },
    ...routes,
  })
  const router = createMemoryRouter(
    [
      { path: '/', element: <Home /> },
      { path: '/anime/:id', element: <p>show page</p> },
      { path: '/watch/:episodeId', element: <p>player</p> },
      { path: '/schedule', element: <p>schedule page</p> },
      { path: '/search', element: <p>browse page</p> },
      { path: '/recs', element: <p>recs page</p> },
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

/** A shelf, by its heading: `Shelf` renders one `<section>` per heading. */
function shelf(name: string): HTMLElement {
  const heading = screen.getByRole('heading', { level: 2, name })
  return heading.closest('section') as HTMLElement
}

/** The white strip on a tile, found by the element that draws the fill. */
function progressWidth(tile: HTMLElement): string | undefined {
  const track = tile.querySelector('.absolute.inset-x-0.bottom-0')
  return (track?.firstElementChild as HTMLElement | null)?.style.width
}

/** Which column is "today" for the test user, so a fixture can land on it. */
const TODAY = weekdayInTimezone(TEST_USER.timezone)

/** One followed show airing tonight, one airing on another day. */
function weekSchedule(): SchedulePage {
  const other = (TODAY + 2) % 7
  return {
    ...EMPTY_SCHEDULE,
    days: EMPTY_SCHEDULE.days.map((day) => {
      if (day.weekday === TODAY) {
        return {
          ...day,
          entries: [
            scheduleEntry(FRIEREN, {
              air_time_local: '20:00',
              next_episode: 12,
              next_at: '2099-01-01T19:00:00Z',
              following: true,
              list_status: 'watching',
            }),
          ],
        }
      }
      if (day.weekday === other) {
        return {
          ...day,
          entries: [
            scheduleEntry(APOTHECARY, {
              air_time_local: '22:00',
              next_episode: 2,
              following: true,
              list_status: 'watching',
            }),
            // Not on the viewer's list: the shelf is appointments, not a season.
            scheduleEntry({
              ...APOTHECARY,
              id: 999,
              title: { ...APOTHECARY.title, preferred: 'Unfollowed' },
            }),
          ],
        }
      }
      return day
    }),
  }
}

/** A ready, unwatched episode of the show that aired this week. */
function readyEpisode(overrides: Partial<EpisodeOut> = {}): EpisodeOut {
  return {
    id: 9301,
    number: 7,
    title: 'The Land Where Souls Rest',
    air_at: '2026-09-04T14:00:00Z',
    air_at_estimated: false,
    aired: true,
    still_url: 'https://example.test/still-7.jpg',
    state: 'ready',
    watched: false,
    download_progress: null,
    prepare_progress: null,
    failure_reason: null,
    unavailable_reason: null,
    release: null,
    rendition: null,
    ...overrides,
  }
}

const READY_ONLY = {
  continue_watching: [],
  behind: [],
  new_this_week: [{ anime: FRIEREN, episode: readyEpisode() }],
}

/* --- The season the hero is built from -------------------------------- */

/*
 * `EMPTY_SCHEDULE` is Fall 2026, so everything below is a Fall 2026 show. The
 * viewer's list is one entry — Frieren, scored 9, genres Adventure / Drama /
 * Fantasy — which makes those three the top three by weight.
 */

/**
 * Two of the viewer's top three genres, and barely anybody watching it: the
 * show that proves overlap is weighed above popularity.
 */
const KAIJU: AnimeSummary = {
  ...APOTHECARY,
  id: 700001,
  title: {
    romaji: 'Kaijuu no Hanayome',
    english: 'The Kaiju’s Bride',
    native: '怪獣の花嫁',
    preferred: 'The Kaiju’s Bride',
  },
  genres: ['Adventure', 'Fantasy', 'Romance'],
  studio: 'Bones',
  episodes: 12,
  popularity: 900,
  average_score: 74,
  banner_url: 'https://example.test/kaiju-banner.jpg',
  cover_large_url: 'https://example.test/kaiju-large.jpg',
}

/** One genre in common (Drama). Not enough to be offered. */
const HALF_MATCH: AnimeSummary = {
  ...APOTHECARY,
  id: 700002,
  title: { ...APOTHECARY.title, preferred: 'Half a Match' },
  genres: ['Drama', 'Mystery'],
}

/**
 * The ordinary MAL-sourced case: a cover and no banner at all. The frame has
 * nothing 21:9 to fill itself with, which is what the poster hero is for.
 */
const POSTER_ONLY: AnimeSummary = {
  ...APOTHECARY,
  id: 700005,
  title: { ...APOTHECARY.title, preferred: 'No Banner Here' },
  cover_url: 'https://example.test/no-banner.jpg',
  cover_large_url: 'https://example.test/no-banner-large.jpg',
}

/** The model's own pick, for a show airing this season. Not in the grid. */
const SEASON_PICK: AnimeSummary = {
  ...APOTHECARY,
  id: 700003,
  title: { ...APOTHECARY.title, preferred: 'Thousand Autumns' },
  genres: ['Mystery'],
  banner_url: 'https://example.test/autumns-banner.jpg',
}

/** A pick for a show that aired years ago: in the run, never in the hero. */
const OLD_PICK: AnimeSummary = { ...FRIEREN, id: 700004 }

/** A Fall 2026 row, since `EMPTY_SCHEDULE` is that season. */
function seasonRow(
  id: number,
  preferred: string,
  overrides: Partial<AnimeSummary> = {},
): AnimeSummary {
  return { ...APOTHECARY, id, title: { ...APOTHECARY.title, preferred }, ...overrides }
}

/**
 * Two rows as the MAL fallback caches them: no genres until a detail fetch
 * reaches them, which is the production case that emptied the hero. They
 * differ only in how many people are watching, which is what has to order
 * them once there are no genres to go on.
 */
const VIA_MAL = seasonRow(700006, 'Cached via MAL', {
  genres: [],
  source: 'mal',
  popularity: 4_000,
})
const VIA_MAL_OTHER = seasonRow(700007, 'Also via MAL', {
  genres: [],
  source: 'mal',
  popularity: 120_000,
})

/** Two more in-season picks, so a run can try to claim more than its two. */
const PICK_TWO = seasonRow(700008, 'Second Pick', {
  banner_url: 'https://example.test/second-banner.jpg',
})
const PICK_THREE = seasonRow(700009, 'Third Pick', {
  banner_url: 'https://example.test/third-banner.jpg',
})

/*
 * Three season rows with no genre in common with the viewer and no banner, so
 * popularity alone decides the order they are offered in — including the row
 * the catalogue has no number for at all.
 */
const CROWD_FAVOURITE = seasonRow(700010, 'Everyone Is Watching', {
  genres: [],
  popularity: 300_000,
  banner_url: null,
})
const MIDDLING = seasonRow(700011, 'Some Are Watching', {
  genres: [],
  popularity: 6_000,
  banner_url: null,
})
const UNRANKED = seasonRow(700012, 'Nobody Has Said', {
  genres: [],
  popularity: null,
  average_score: null,
  banner_url: null,
})

function seasonSchedule(...shows: AnimeSummary[]): SchedulePage {
  return {
    ...EMPTY_SCHEDULE,
    days: EMPTY_SCHEDULE.days.map((day) =>
      day.weekday === 0 ? { ...day, entries: shows.map((show) => scheduleEntry(show)) } : day,
    ),
  }
}

function recsWith(...picks: AnimeSummary[]): RecsPage {
  return {
    ...RECS_PAGE_EMPTY,
    run: {
      id: 9,
      prompt: null,
      created_at: '2026-09-10T08:00:00Z',
      model: 'claude-opus-5',
      candidate_count: 12,
      picks: picks.map((anime) => ({ anime, case: 'Because you liked the other one.' })),
    },
  }
}

/** One entry, scored, carrying the genres the matcher weighs. */
const MY_LIST: MyListItem[] = [
  { anime: FRIEREN, entry: listEntry({ status: 'watching', score: 9 }) },
]

/** The hero's own frame, by the label the carousel carries. */
function hero(): HTMLElement {
  return screen.getByRole('region', { name: 'Recommended this season' })
}

function heroTitle(): string {
  return screen.getByRole('heading', { level: 1 }).textContent ?? ''
}

/** The one frame the hero draws, whichever treatment it landed on. */
function heroFrame(): HTMLElement {
  return hero().querySelector('.shadow-hero') as HTMLElement
}

/**
 * jsdom decodes nothing, and the hero measures its banner off-frame before
 * deciding what to do with it: the intrinsic size has to be planted on that
 * copy and the load announced by hand. A 16:9 backdrop is the shape that
 * fills the frame; AniList's ~1900×400 is the shape that does not.
 */
function loadBanner(url: string, width = 1920, height = 1080): void {
  const image = hero().querySelector(`img[src="${url}"]`) as HTMLImageElement
  Object.defineProperty(image, 'naturalWidth', { value: width, configurable: true })
  Object.defineProperty(image, 'naturalHeight', { value: height, configurable: true })
  fireEvent.load(image)
}

/**
 * The shows the carousel holds, in order, read off the dots — each of which
 * is labelled with the show it jumps to. Only meaningful for more than one
 * show, since a single-show hero renders no controls at all.
 */
function heroSlides(): string[] {
  const chevrons = ['Previous recommendation', 'Next recommendation']
  const controls = screen.getByRole('button', { name: chevrons[0] }).parentElement as HTMLElement

  return within(controls)
    .getAllByRole('button')
    .map((dot) => dot.getAttribute('aria-label') ?? '')
    .filter((label) => !chevrons.includes(label))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Watch Now hero', () => {
  it('leads with the run’s in-season picks, then ranks the season by genre overlap', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(HALF_MATCH, KAIJU) },
      'GET /api/recs': { body: recsWith(SEASON_PICK, OLD_PICK) },
      'GET /api/list': { body: MY_LIST },
    })

    expect(await screen.findByText('Recommended this season')).toBeInTheDocument()
    // The pick the model argued for, and which airs this season, comes first.
    expect(heroTitle()).toBe(SEASON_PICK.title.preferred)

    // Then the season, best overlap first — the one-genre show is ranked
    // last, not turned away. The pick for a 2023 show is out: wrong season.
    expect(heroSlides()).toEqual([
      SEASON_PICK.title.preferred,
      KAIJU.title.preferred,
      HALF_MATCH.title.preferred,
    ])
    expect(
      within(hero()).queryByRole('button', { name: OLD_PICK.title.preferred }),
    ).not.toBeInTheDocument()
  })

  it('still offers the season when its rows have no genres yet', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      // Everything the MAL fallback cached: a season with no genres on it at
      // all, under a viewer whose list has plenty (the production report).
      'GET /api/schedule': { body: seasonSchedule(VIA_MAL, VIA_MAL_OTHER) },
      'GET /api/recs': { body: recsWith(SEASON_PICK) },
      'GET /api/list': { body: MY_LIST },
    })

    await screen.findByText('Recommended this season')
    // With no genres to weigh, popularity orders them — so the grid's own
    // order is not what comes back.
    expect(heroSlides()).toEqual([
      SEASON_PICK.title.preferred,
      VIA_MAL_OTHER.title.preferred,
      VIA_MAL.title.preferred,
    ])
  })

  it('ranks the season by popularity under genre overlap, with nulls last', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      // Grid order is deliberately none of the three orders below.
      'GET /api/schedule': {
        body: seasonSchedule(MIDDLING, UNRANKED, CROWD_FAVOURITE, KAIJU),
      },
      'GET /api/list': { body: MY_LIST },
    })

    await screen.findByText('Recommended this season')
    expect(heroSlides()).toEqual([
      // Two shared genres beat three hundred thousand viewers.
      KAIJU.title.preferred,
      CROWD_FAVOURITE.title.preferred,
      MIDDLING.title.preferred,
      // A show the catalogue has no number for is not the least popular show
      // in the season, but it cannot be ranked, so it goes last.
      UNRANKED.title.preferred,
    ])
  })

  it('never gives the run more than two of the six slides', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(KAIJU) },
      'GET /api/recs': { body: recsWith(SEASON_PICK, PICK_TWO, PICK_THREE) },
      'GET /api/list': { body: MY_LIST },
    })

    await screen.findByText('Recommended this season')
    // Two picks, then the season. The third pick waits for the next run.
    expect(heroSlides()).toEqual([
      SEASON_PICK.title.preferred,
      PICK_TWO.title.preferred,
      KAIJU.title.preferred,
    ])
    expect(
      within(hero()).queryByRole('button', { name: PICK_THREE.title.preferred }),
    ).not.toBeInTheDocument()
  })

  it('says what the show is, in one line, over its widest artwork', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(KAIJU) },
      'GET /api/list': { body: MY_LIST },
    })

    expect(await screen.findByText('Recommended this season')).toBeInTheDocument()
    expect(heroTitle()).toBe(KAIJU.title.preferred)
    expect(screen.getByText(KAIJU.title.native as string)).toBeInTheDocument()
    expect(
      screen.getByText('Bones · Fall 2026 · 12 episodes · Adventure, Fantasy, Romance'),
    ).toBeInTheDocument()
    // banner → cover_large → cover, and this show has a banner the frame can
    // be filled with once its shape is known.
    loadBanner(KAIJU.banner_url as string)
    expect(hero().querySelector('img')).toHaveAttribute('src', KAIJU.banner_url)
    expect(screen.getByRole('link', { name: 'Details' })).toHaveAttribute(
      'href',
      `/anime/${String(KAIJU.id)}`,
    )
    // One show is not a carousel.
    expect(screen.queryByRole('button', { name: 'Next recommendation' })).not.toBeInTheDocument()
  })

  it('steps through the shows with the chevrons and the dots', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(KAIJU) },
      'GET /api/recs': { body: recsWith(SEASON_PICK) },
      'GET /api/list': { body: MY_LIST },
    })

    await screen.findByText('Recommended this season')
    const user = userEvent.setup()
    expect(heroTitle()).toBe(SEASON_PICK.title.preferred)

    await user.click(screen.getByRole('button', { name: 'Next recommendation' }))
    expect(heroTitle()).toBe(KAIJU.title.preferred)

    // Wraps rather than stopping dead at the end.
    await user.click(screen.getByRole('button', { name: 'Next recommendation' }))
    expect(heroTitle()).toBe(SEASON_PICK.title.preferred)

    await user.click(screen.getByRole('button', { name: 'Previous recommendation' }))
    expect(heroTitle()).toBe(KAIJU.title.preferred)

    // And the dots jump straight to one.
    await user.click(within(hero()).getByRole('button', { name: SEASON_PICK.title.preferred }))
    expect(heroTitle()).toBe(SEASON_PICK.title.preferred)
    expect(
      within(hero()).getByRole('button', { name: SEASON_PICK.title.preferred }),
    ).toHaveAttribute('aria-current', 'true')
  })

  it('keeps the chevrons off the artwork, in the action row beside the dots', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(KAIJU) },
      'GET /api/recs': { body: recsWith(SEASON_PICK) },
      'GET /api/list': { body: MY_LIST },
    })

    await screen.findByText('Recommended this season')
    const previous = screen.getByRole('button', { name: 'Previous recommendation' })
    const next = screen.getByRole('button', { name: 'Next recommendation' })
    const firstDot = within(hero()).getByRole('button', { name: SEASON_PICK.title.preferred })
    const lastDot = within(hero()).getByRole('button', { name: KAIJU.title.preferred })

    // Not over the eyebrow, the title or the art: the frame holds the image
    // and the title block, and nothing else.
    const frame = hero().querySelector('img')?.parentElement as HTMLElement
    expect(frame).not.toContainElement(previous)
    expect(frame).not.toContainElement(next)

    // One control, read left to right: ‹ • • ›
    const controls = previous.parentElement as HTMLElement
    expect(controls).toContainElement(firstDot)
    expect(controls).toContainElement(next)
    expect(previous.compareDocumentPosition(firstDot)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(lastDot.compareDocumentPosition(next)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('puts the show on the list as planned, and then says so', async () => {
    const fetchMock = renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(KAIJU) },
      'GET /api/list': { body: MY_LIST },
      'PUT /api/list/700001': {
        body: listEntry({ anime_id: KAIJU.id, status: 'planned', progress: 0 }),
      },
    })

    await screen.findByText('Recommended this season')
    await userEvent.click(screen.getByRole('button', { name: 'Add to list' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain('PUT /api/list/700001')
    })
    expect(jsonBodyOf(callTo(fetchMock, '/api/list/700001'))).toEqual({ status: 'planned' })

    // The card stays where it was and changes what it offers.
    expect(await screen.findByRole('button', { name: 'On your list' })).toBeDisabled()
    expect(heroTitle()).toBe(KAIJU.title.preferred)
  })

  it('falls back to the season itself when the list has no genres to go on', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(HALF_MATCH, KAIJU) },
      'GET /api/list': { body: [] },
    })

    await screen.findByText('Recommended this season')
    // Nothing to filter by, so both shows are offered — the one with a banner
    // leads, since that is the only art the frame can be filled with.
    expect(heroTitle()).toBe(KAIJU.title.preferred)
    expect(
      within(hero()).getByRole('button', { name: HALF_MATCH.title.preferred }),
    ).toBeInTheDocument()
  })

  it('leads with the shows that have a banner, and frames the rest as posters', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      // Season order puts the banner-less show first; the hero reorders it.
      'GET /api/schedule': { body: seasonSchedule(HALF_MATCH, KAIJU) },
      'GET /api/list': { body: [] },
    })

    await screen.findByText('Recommended this season')
    const dots = within(hero()).getAllByRole('button', { name: /Kaiju|Half/ })
    expect(dots.map((dot) => dot.getAttribute('aria-label'))).toEqual([
      KAIJU.title.preferred,
      HALF_MATCH.title.preferred,
    ])

    // A 16:9 banner fills the frame, and nothing is blurred.
    loadBanner(KAIJU.banner_url as string)
    expect(hero().querySelector('img')).toHaveAttribute('src', KAIJU.banner_url)
    expect(hero().querySelector('[data-hero-backdrop]')).toBeNull()
  })

  it('keeps one frame of the same size on every slide', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(KAIJU, SEASON_PICK, POSTER_ONLY) },
      'GET /api/list': { body: [] },
    })

    await screen.findByText('Recommended this season')
    const user = userEvent.setup()
    expect(heroSlides()).toHaveLength(3)

    // Three shows, three treatments: a 16:9 banner that fills the frame, an
    // AniList strip that cannot, and a show with no banner at all.
    const shapes = new Set<string>()
    const shows = [KAIJU, SEASON_PICK, POSTER_ONLY]
    // AniList's strip for the season pick, a TMDB-shaped backdrop for the rest.
    const shape = (anime: AnimeSummary): [number, number] =>
      anime.id === SEASON_PICK.id ? [1900, 400] : [1920, 1080]

    for (const title of heroSlides()) {
      const anime = shows.find((show) => show.title.preferred === title) as AnimeSummary
      if (anime.banner_url != null) loadBanner(anime.banner_url, ...shape(anime))

      // Only the backdrop-shaped banner fills the frame; the other two are
      // washed — and the frame is the same box in all three cases.
      const washed = hero().querySelector('[data-hero-backdrop]') !== null
      expect(washed).toBe(anime.id !== KAIJU.id)

      const frame = heroFrame()
      shapes.add(`${frame.className}|${frame.style.aspectRatio}`)
      await user.click(screen.getByRole('button', { name: 'Next recommendation' }))
    }

    // One frame, one size — the owner's "all heroes need to be in the same
    // size" (2026-09-12). Nothing overrides the 21:9 with a measured ratio.
    expect(shapes.size).toBe(1)
    expect([...shapes][0]).toContain('aspect-[21/9]')
    expect([...shapes][0]).toMatch(/\|$/)
  })

  it('never stretches a poster across the frame when a show has no banner', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': { body: seasonSchedule(POSTER_ONLY) },
      'GET /api/list': { body: [] },
    })

    await screen.findByText('Recommended this season')
    expect(heroTitle()).toBe(POSTER_ONLY.title.preferred)

    // A wash rather than a picture: the cover blurred and darkened to fill the
    // 21:9 frame, with the crisp 2:3 poster laid over it beside the title.
    const backdrop = hero().querySelector('[data-hero-backdrop]')
    expect(backdrop).toHaveAttribute('src', POSTER_ONLY.cover_large_url)
    expect(backdrop).toHaveClass('blur-[40px]')

    const poster = hero().querySelector('[data-hero-poster]') as HTMLElement
    expect(poster.querySelector('img')).toHaveAttribute('src', POSTER_ONLY.cover_large_url)
    expect(poster.firstElementChild).toHaveClass('aspect-[2/3]')
  })

  it('leaves out shows the viewer already follows', async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/schedule': {
        body: seasonSchedule({ ...KAIJU, list_status: 'watching' }, HALF_MATCH),
      },
      'GET /api/list': { body: [] },
    })

    await screen.findByText('Recommended this season')
    expect(heroTitle()).toBe(HALF_MATCH.title.preferred)
    expect(
      within(hero()).queryByRole('button', { name: KAIJU.title.preferred }),
    ).not.toBeInTheDocument()
  })

  it('heads the page instead of framing nothing when the season offers none', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE } })

    expect(await screen.findByRole('heading', { level: 1, name: 'Watch Now' })).toBeInTheDocument()
    expect(screen.queryByText('Recommended this season')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('region', { name: 'Recommended this season' }),
    ).not.toBeInTheDocument()
  })
})

describe('the episode card’s art (owner, 2026-09-12)', () => {
  /** The tile on "Continue watching", which is the card under test. */
  function tile(): HTMLElement {
    return within(shelf('Continue watching')).getAllByRole('link')[0] as HTMLElement
  }

  /** The off-frame copy of the banner the card measures before showing it. */
  function probe(): HTMLImageElement {
    return tile().querySelector(`img[src="${String(FRIEREN.banner_url)}"]`) as HTMLImageElement
  }

  /** jsdom decodes nothing: the intrinsic size is planted and the load announced. */
  function load(image: HTMLImageElement, width: number, height: number): void {
    Object.defineProperty(image, 'naturalWidth', { value: width, configurable: true })
    Object.defineProperty(image, 'naturalHeight', { value: height, configurable: true })
    fireEvent.load(image)
  }

  async function renderCard(episode: Partial<EpisodeOut> = {}): Promise<void> {
    renderHome({
      'GET /api/home': {
        body: {
          ...EMPTY_HOME,
          continue_watching: [
            { ...CONTINUE_FRIEREN, episode: { ...CONTINUE_FRIEREN.episode, ...episode } },
          ],
        },
      },
    })
    await screen.findByRole('heading', { level: 2, name: 'Continue watching' })
  }

  it('shows the episode’s own still when TMDB has one', async () => {
    await renderCard({ still_url: 'https://example.test/still-5.jpg' })

    const images = tile().querySelectorAll('img')
    expect(images).toHaveLength(1)
    expect(images[0]).toHaveAttribute('src', 'https://example.test/still-5.jpg')
    // Nothing to frame around: the still is the picture.
    expect(tile().querySelector('[data-hero-backdrop]')).toBeNull()
  })

  it('frames the poster rather than zooming into a 4.75:1 banner', async () => {
    await renderCard()

    // AniList ships ~1900 × 400; `object-cover` in a 16:9 card shows a sliver.
    load(probe(), 1900, 400)

    expect(tile().querySelector(`img[src="${String(FRIEREN.banner_url)}"]`)).toBeNull()
    const backdrop = tile().querySelector('[data-hero-backdrop]')
    expect(backdrop).toHaveAttribute('src', FRIEREN.cover_large_url)
    expect(backdrop).toHaveClass('blur-[40px]')

    // And the crisp poster over the wash, at its own 2:3 ratio.
    const poster = tile().querySelector('[data-hero-poster]') as HTMLElement
    expect(poster.querySelector('img')).toHaveAttribute('src', FRIEREN.cover_large_url)
    expect(poster.firstElementChild).toHaveClass('aspect-[2/3]')
    // The card is the same card: still 16:9, still carrying its strip.
    expect(tile().firstElementChild).toHaveClass('aspect-[16/9]')
    expect(progressWidth(tile())).toMatch(/^52\.5/)
  })

  it('fills the card with a banner that is a 16:9 backdrop', async () => {
    await renderCard()

    load(probe(), 1920, 1080)

    const images = tile().querySelectorAll('img')
    expect(images).toHaveLength(1)
    expect(images[0]).toHaveAttribute('src', FRIEREN.banner_url)
    expect(tile().querySelector('[data-hero-backdrop]')).toBeNull()
  })
})

describe('Watch Now shelves', () => {
  it('opens with what the viewer stopped in the middle of (FR-W1)', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE_CONTINUE } })

    await screen.findByRole('heading', { level: 2, name: 'Continue watching' })
    const tiles = within(shelf('Continue watching')).getAllByRole('link')
    expect(tiles).toHaveLength(1)

    const tile = tiles[0] as HTMLElement
    expect(tile).toHaveAccessibleName(`Resume ${FRIEREN.title.preferred} episode 5`)
    expect(tile).toHaveAttribute('href', `/watch/${String(CONTINUE_FRIEREN.episode.id)}`)
    expect(within(tile).getByText('Episode 5 · 11 min left')).toBeInTheDocument()
    // 754 s into 1436: the white strip says how far, to the pixel.
    expect(progressWidth(tile)).toMatch(/^52\.5/)
    // No episode still on this one, and nothing yet knows what shape the
    // show's banner is: the poster treatment holds the card until it does.
    expect(tile.querySelector('[data-hero-backdrop]')).toHaveAttribute(
      'src',
      FRIEREN.cover_large_url,
    )
  })

  it('says what it can when nothing recorded the episode’s length', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE_CONTINUE_NO_DURATION } })

    await screen.findByRole('heading', { level: 2, name: 'Continue watching' })
    const tile = within(shelf('Continue watching')).getAllByRole('link')[0] as HTMLElement
    expect(within(tile).getByText('Episode 5 · In progress')).toBeInTheDocument()
    // No percentage invented from a length nobody knows.
    expect(progressWidth(tile)).toBeUndefined()
  })

  it('keeps started episodes out of "Ready to watch"', async () => {
    renderHome({
      'GET /api/home': {
        body: {
          ...EMPTY_HOME,
          continue_watching: [CONTINUE_FRIEREN],
          new_this_week: [
            // The same episode the viewer is part way through.
            { anime: FRIEREN, episode: readyEpisode({ id: 9201, number: 5 }) },
            { anime: APOTHECARY, episode: readyEpisode({ id: 9302, number: 2 }) },
          ],
        },
      },
    })

    await screen.findByRole('heading', { level: 2, name: 'Ready to watch' })
    const ready = within(shelf('Ready to watch')).getAllByRole('link')

    expect(ready).toHaveLength(1)
    expect(ready[0]).toHaveAccessibleName(`Play ${APOTHECARY.title.preferred} episode 2`)
    // "Play", not "Resume": no position, so no clock and no strip either.
    expect(within(ready[0] as HTMLElement).getByText('Episode 2')).toBeInTheDocument()
    expect(progressWidth(ready[0] as HTMLElement)).toBeUndefined()
    // Episode art first: this one has a still of its own.
    expect((ready[0] as HTMLElement).querySelector('img')).toHaveAttribute(
      'src',
      'https://example.test/still-7.jpg',
    )

    // And the started one is on the shelf above, once.
    expect(within(shelf('Continue watching')).getAllByRole('link')).toHaveLength(1)
  })

  it('shows no "Continue watching" shelf when nothing was started', async () => {
    renderHome({ 'GET /api/home': { body: READY_ONLY } })

    await screen.findByRole('heading', { level: 2, name: 'Ready to watch' })
    expect(screen.queryByRole('heading', { name: 'Continue watching' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Play .* episode 7/ })).toHaveAttribute(
      'href',
      '/watch/9301',
    )
  })

  it('has no hero and no shelves when the list is empty', async () => {
    renderHome({ 'GET /api/home': { body: EMPTY_HOME } })

    expect(await screen.findByText(/Add a show from Browse/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Browse the catalogue' })).toHaveAttribute(
      'href',
      '/search',
    )
    expect(screen.getByRole('heading', { level: 1, name: 'Watch Now' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 2 })).not.toBeInTheDocument()
  })

  it('marks tonight’s broadcast and nothing else', async () => {
    renderHome({
      // Nothing has aired yet this week, so every card is still an appointment.
      'GET /api/home': { body: { ...HOME_PAGE_CONTINUE, new_this_week: [] } },
      'GET /api/schedule': { body: weekSchedule() },
    })

    await screen.findByRole('heading', { level: 2, name: 'This week' })
    const week = within(shelf('This week'))

    expect(week.getByText('Tonight')).toBeInTheDocument()
    expect(week.getAllByText('Tonight')).toHaveLength(1)
    expect(week.getByText(WEEKDAY_LABELS[(TODAY + 2) % 7] as string)).toBeInTheDocument()
    expect(week.getByText('20:00')).toBeInTheDocument()
    expect(week.getByText('Episode 12')).toBeInTheDocument()
    // Still to come, so Arc says when it will go looking.
    expect(week.getByText('Arc will search at 20:00')).toBeInTheDocument()
    // A show the viewer does not follow keeps no appointment.
    expect(week.queryByText('Unfollowed')).not.toBeInTheDocument()
    expect(week.getByRole('link', { name: 'Full schedule' })).toHaveAttribute('href', '/schedule')
  })

  it('says an episode is ready when the week’s aggregate has the file', async () => {
    renderHome({
      'GET /api/home': { body: { ...READY_ONLY, continue_watching: [] } },
      'GET /api/schedule': { body: weekSchedule() },
    })

    await screen.findByRole('heading', { level: 2, name: 'This week' })
    expect(within(shelf('This week')).getByText('Ready')).toBeInTheDocument()
  })

  it('counts what has piled up in prose, with no badge on the artwork', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE } })

    await screen.findByRole('heading', { level: 2, name: 'Catch up' })
    const catchUp = within(shelf('Catch up'))

    expect(catchUp.getByText('Fall 2023 · 4 episodes behind')).toBeInTheDocument()
    expect(catchUp.getByRole('link', { name: /Frieren/ })).toHaveAttribute(
      'href',
      `/anime/${String(FRIEREN.id)}`,
    )
    // The design keeps the status control off the tile.
    expect(catchUp.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('shows the latest recommendation run’s picks', async () => {
    renderHome({
      'GET /api/home': { body: HOME_PAGE },
      'GET /api/recs': { body: RECS_PAGE },
    })

    await screen.findByRole('heading', { level: 2, name: 'Picked for you' })
    const picks = within(shelf('Picked for you'))
    expect(picks.getByText('From your latest recommendation run.')).toBeInTheDocument()
    expect(picks.getByRole('link', { name: 'Recommendations' })).toHaveAttribute('href', '/recs')
  })

  it('hides the picks shelf when no run has ever been made', async () => {
    renderHome({ 'GET /api/home': { body: HOME_PAGE } })

    await screen.findByRole('heading', { level: 2, name: 'Catch up' })
    expect(screen.queryByRole('heading', { name: 'Picked for you' })).not.toBeInTheDocument()
  })
})

describe('Watch Now failures', () => {
  it('reports a failure to build the page', async () => {
    renderHome({ 'GET /api/home': { status: 500, body: { detail: 'boom' } } })

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load your home page.')
  })

  it('offers a retry that asks for the page again', async () => {
    const fetchMock = renderHome({ 'GET /api/home': { status: 500, body: { detail: 'boom' } } })

    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === 'GET /api/home')).toHaveLength(2)
    })
  })

  it('still shows the API status when /api/health responds', async () => {
    renderHome({ 'GET /api/home': { body: EMPTY_HOME } })

    await screen.findByText(/Add a show from Browse/)

    expect(await screen.findByText('API: ok')).toBeInTheDocument()
    expect(screen.getByText('0.1.0 · dev')).toBeInTheDocument()
    // No TMDB key on this deployment, so no attribution line to carry.
    expect(screen.queryByText(/not endorsed or certified by TMDB/)).not.toBeInTheDocument()
  })

  it("carries TMDB's attribution when the deployment has a key", async () => {
    renderHome({
      'GET /api/home': { body: EMPTY_HOME },
      'GET /api/health': { body: { ...HEALTH, tmdb_enabled: true } },
    })

    await screen.findByText(/Add a show from Browse/)
    expect(
      await screen.findByText(
        'This product uses the TMDB API but is not endorsed or certified by TMDB.',
      ),
    ).toBeInTheDocument()
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
