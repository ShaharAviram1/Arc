import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import {
  Artwork,
  Button,
  buttonClass,
  cx,
  EmptyState,
  Eyebrow,
  FOCUS_RING,
  HeroFrame,
  Shelf,
  Skeleton,
} from '@/components/ui'
import {
  bannerArt,
  catalogErrorMessage,
  episodeStateLabel,
  hasBanner,
  heroArt,
  keyVisual,
  listErrorMessage,
  useMyList,
  useSetListEntry,
  type AnimeSummary,
  type EpisodeOut,
  type MyListItem,
} from '@/lib/anime'
import { useMe } from '@/lib/auth'
import { useHealth } from '@/lib/health'
import { usePrefersReducedMotion } from '@/lib/media'
import { useRecs, type RecsPage } from '@/lib/recs'
import {
  seasonLabel,
  SEASONS,
  useHome,
  useSchedule,
  weekdayInTimezone,
  WEEKDAY_LABELS,
  type BehindEntry,
  type HomePage,
  type Season,
  type SchedulePage,
  type SeasonRef,
} from '@/lib/schedule'

/**
 * Watch Now (spec §4.6 FR-W1, §5; design handoff "Watch Now").
 *
 * The hero is what the season has to offer this viewer — not what they were
 * last doing (owner, 2026-09-11). A page whose biggest object is the episode
 * you paused says "finish your homework"; one that opens with a show you have
 * not heard of and might like is the reason to open a catalogue at all. What
 * you were doing is still the first thing under it, as a shelf of its own.
 *
 * So: one cycling hero of season recommendations, then the shelves in the
 * order a week actually runs — what is half-watched, what is ready and
 * unstarted, what is coming this week, what has piled up, and what the last
 * recommendation run suggested. Every shelf hides itself when it is empty,
 * because a page of empty shelves says "broken", not "quiet week".
 *
 * The data is the same three-section `/api/home` aggregate as before, plus the
 * season grid the Schedule page already asks for, the stored recommendation
 * run the Recs page already holds, and the viewer's list as My List already
 * fetches it — four shared caches, no new call shape and no new endpoint.
 */

/** Where you stopped, then the first thing that is ready. Eight is a shelf. */
const SHELF_LIMIT = 8

/** Enough appointment cards to cover a week without turning into a schedule. */
const THIS_WEEK_LIMIT = 12

const CONTINUE_LEDE = 'Where you stopped. Picks up at the second you left.'
const READY_LEDE = 'Arc has the file and you have not started it yet.'
const THIS_WEEK_LEDE = 'Seasonal anime keeps appointments. Times are in your timezone.'
const CATCH_UP_LEDE = 'Falling behind is normal. Arc keeps them ready.'
const PICKS_LEDE = 'From your latest recommendation run.'

const EMPTY_TITLE = 'Nothing here yet'
const EMPTY_MESSAGE =
  'Nothing here yet. Add a show from Browse, or connect MyAnimeList and Arc will import your ' +
  'list — then this page fills up with what you are watching.'

const HOME_ERROR = 'Could not load your home page.'

/* --- Health ------------------------------------------------------------ */

function HealthBadge() {
  const { data, isPending, isError } = useHealth()

  if (isPending) {
    return <span className="text-[var(--arc-text-muted)]">API: checking…</span>
  }
  if (isError || !data) {
    return <span className="text-[var(--arc-error)]">API: unreachable</span>
  }
  return (
    <>
      <span className="text-[var(--arc-ok)]">API: {data.status}</span>
      <span className="ml-2 text-[var(--arc-text-muted)]">
        {data.version} · {data.env}
      </span>
    </>
  )
}

/* --- Season recommendations (the hero) --------------------------------- */

/** How many shows cycle through the hero at most. */
const HERO_LIMIT = 6

/** How many of the viewer's own genres a season show is matched against. */
const TOP_GENRE_COUNT = 3

/**
 * How many of the six slides the recommendation run may have (owner,
 * 2026-09-11, from production).
 *
 * The run is the same handful of shows until somebody asks for another one, so
 * a hero that is entirely last night's batch stops being a reason to look at
 * the season at all — which is what the owner saw: season rows cached through
 * the MAL fallback carry no genres until a detail fetch reaches them, so the
 * genre rule matched nothing, and the "list has no genres" escape hatch did
 * not fire because the *list* had plenty. Two slides lets the argued picks
 * lead without owning the frame, and the season is now ranked rather than
 * filtered, so it can always fill the rest.
 */
const HERO_PICK_LIMIT = 2

/** How long a hero slide holds. Long enough to read the meta line twice. */
const HERO_INTERVAL_MS = 8000

/** An entry with no score still says something; it says it from the middle. */
const NEUTRAL_SCORE = 5

/**
 * How many banner shows the season has to offer before a banner-less one is
 * turned away entirely (owner, 2026-09-11).
 *
 * The hero is the largest thing on the page and a banner is the only artwork
 * that fills it at its own resolution, so given the choice the carousel takes
 * the shows that have one. `HeroFrame` frames the rest honestly rather than
 * badly — see the poster hero — but a season that has three good frames to
 * offer has no reason to spend a slide on a lesser one. Below three it does:
 * a one-slide carousel of the only banner in the season is a worse page than
 * three real recommendations.
 */
const MIN_BANNER_HEROES = 3

const HERO_EYEBROW = 'Recommended this season'
const ADD_LABEL = 'Add to list'
const ADDED_LABEL = 'On your list'

/** True when a show belongs to exactly the season named. */
function inSeason(anime: AnimeSummary, ref: SeasonRef): boolean {
  return anime.season_year === ref.year && anime.season === ref.season
}

/**
 * The genres the viewer's own list argues for, best first.
 *
 * Weighted by score, since a 9 is a stronger statement than a 6, and an
 * unscored entry counts from the middle of the scale rather than not at all —
 * a list imported from MAL is mostly unscored, and ignoring it would leave the
 * hero with nothing to reason from. Dropped shows are left out: they are
 * evidence against a genre, not for it.
 *
 * Ties break on the genre name so the same list always yields the same three;
 * a hero that reshuffles itself between renders is a bug people cannot report.
 */
function topGenres(list: MyListItem[]): string[] {
  const weights = new Map<string, number>()

  for (const item of list) {
    if (item.entry.status === 'dropped') continue
    const weight = item.entry.score ?? NEUTRAL_SCORE
    for (const genre of item.anime.genres ?? []) {
      weights.set(genre, (weights.get(genre) ?? 0) + weight)
    }
  }

  return [...weights.entries()]
    .sort(([leftGenre, left], [rightGenre, right]) =>
      right === left ? leftGenre.localeCompare(rightGenre) : right - left,
    )
    .slice(0, TOP_GENRE_COUNT)
    .map(([genre]) => genre)
}

/** Every show in the cached season grid, once each, in the server's order. */
function seasonShows(schedule: SchedulePage): AnimeSummary[] {
  const seen = new Set<number>()
  const shows: AnimeSummary[] = []

  for (const day of schedule.days) {
    for (const entry of day.entries) {
      if (seen.has(entry.anime.id)) continue
      seen.add(entry.anime.id)
      shows.push(entry.anime)
    }
  }
  for (const entry of schedule.unscheduled) {
    if (seen.has(entry.anime.id)) continue
    seen.add(entry.anime.id)
    shows.push(entry.anime)
  }
  return shows
}

function genreOverlap(anime: AnimeSummary, genres: string[]): number {
  const own = anime.genres ?? []
  return genres.filter((genre) => own.includes(genre)).length
}

/** The same shows, the ones with a banner first, stable within each group. */
function bannerFirst(candidates: AnimeSummary[]): AnimeSummary[] {
  return [...candidates.filter(hasBanner), ...candidates.filter((anime) => !hasBanner(anime))]
}

/**
 * The same shows, the ones with a banner first, and the rest dropped once
 * there are enough of those to fill a carousel with.
 *
 * Stable within each group, so the argument order above survives: a banner
 * only decides between two shows Arc was equally willing to offer, it never
 * promotes a weaker recommendation over a stronger one.
 */
function preferBanners(candidates: AnimeSummary[]): AnimeSummary[] {
  const banners = candidates.filter(hasBanner)
  if (banners.length >= MIN_BANNER_HEROES) return banners
  return bannerFirst(candidates)
}

/**
 * How many people have the show on a list, or -1 for "the catalogue does not
 * say" — which sorts it below a show with one follower rather than above
 * everything, since an absent number is no evidence of an audience.
 */
function popularityOf(anime: AnimeSummary): number {
  return anime.popularity ?? -1
}

/**
 * The season, best candidate first — ranked, never filtered.
 *
 * Genre overlap with the viewer's own top genres decides it, and a row that
 * carries no genres at all scores zero and sorts last rather than dropping
 * out. That is the fix: every row cached while AniList was down has an empty
 * `genres` until a detail fetch reaches it, and a hero that shows nothing from
 * the season because the catalogue has not been asked yet is worse than one
 * that shows the season in an imperfect order.
 *
 * Below overlap it is popularity, nulls last — the same key the recommendation
 * pool ranks a season by, now that `AnimeSummary` carries it. Which matters
 * most exactly where the genres are missing: a season cached through the MAL
 * fallback ranks on how many people are watching each show rather than on the
 * order the grid happened to be built in. Then a banner (the hero is 21:9, and
 * that is the artwork that fills it), and then the grid's own order, which
 * keeps the set stable between renders.
 */
function rankSeason(shows: AnimeSummary[], genres: string[]): AnimeSummary[] {
  return shows
    .map((anime, order) => ({ anime, order, overlap: genreOverlap(anime, genres) }))
    .sort(
      (left, right) =>
        right.overlap - left.overlap ||
        popularityOf(right.anime) - popularityOf(left.anime) ||
        Number(hasBanner(right.anime)) - Number(hasBanner(left.anime)) ||
        left.order - right.order,
    )
    .map((ranked) => ranked.anime)
}

/**
 * What the hero offers, best argument first.
 *
 * Two sources, and the season is guaranteed a place in the result whenever the
 * grid has an unfollowed show in it. **At most two** slides go to the latest
 * run's picks for something airing now or next — banner-first among those,
 * since they are otherwise equally argued — and the rest of the six come from
 * the season itself, ranked rather than filtered (`rankSeason`). Nothing is
 * turned away for having no genres.
 *
 * Only then is the set cut to six, and only then does artwork get a say: the
 * whole season is weighed on its merits first, so `preferBanners` chooses
 * among everything that qualified rather than among the first six of them.
 * Because the picks are capped at two, it can never drop the season entirely —
 * reaching its three-banner threshold takes at least one banner from the
 * season.
 */
function seasonRecommendations(
  schedule: SchedulePage | undefined,
  recs: RecsPage | undefined,
  list: MyListItem[],
): AnimeSummary[] {
  if (schedule === undefined) return []

  const current: SeasonRef = { year: schedule.year, season: schedule.season }
  const chosen: AnimeSummary[] = []
  const taken = new Set<number>()

  function take(anime: AnimeSummary): void {
    if (taken.has(anime.id)) return
    taken.add(anime.id)
    chosen.push(anime)
  }

  const picks = (recs?.run?.picks ?? [])
    .map((pick) => pick.anime)
    .filter((anime) => inSeason(anime, current) || inSeason(anime, schedule.next))
  for (const anime of bannerFirst(picks).slice(0, HERO_PICK_LIMIT)) take(anime)

  const unfollowed = seasonShows(schedule).filter((anime) => anime.list_status === null)
  for (const anime of rankSeason(unfollowed, topGenres(list))) take(anime)

  return preferBanners(chosen).slice(0, HERO_LIMIT)
}

/**
 * Every show the hero could be showing, by id, whether or not it still
 * qualifies.
 *
 * The hero's line-up is frozen the first time it is non-empty (see `Home`), so
 * adding the show on screen to your list must not make it vanish mid-sentence
 * — but the card still has to read the *current* list status, which is what
 * turns "Add to list" into "On your list". Hence a pool of the unfiltered
 * shows: the order is frozen, the shows themselves are always the fresh ones
 * out of the cache.
 */
function heroPool(
  schedule: SchedulePage | undefined,
  recs: RecsPage | undefined,
): Map<number, AnimeSummary> {
  const pool = new Map<number, AnimeSummary>()

  for (const pick of recs?.run?.picks ?? []) pool.set(pick.anime.id, pick.anime)
  if (schedule !== undefined) {
    for (const anime of seasonShows(schedule)) pool.set(anime.id, anime)
  }
  return pool
}

/** "Fall 2026", or the bare year when the catalogue spells the season oddly. */
function seasonText(anime: AnimeSummary): string | null {
  const { season, season_year } = anime
  if (season_year === null) return null
  if (season !== null && (SEASONS as readonly string[]).includes(season)) {
    return seasonLabel(season_year, season as Season)
  }
  return String(season_year)
}

/**
 * "Madhouse · Fall 2026 · 28 episodes · Adventure, Drama, Fantasy" — the one
 * line under a recommended title. Everything the catalogue does not know is
 * dropped rather than left blank, so a MAL-sourced row gets a short line
 * instead of a row of separators.
 */
function recommendationMeta(anime: AnimeSummary): string {
  const parts: string[] = []

  const { studio, episodes } = anime
  if (studio !== null && studio !== undefined && studio !== '') parts.push(studio)

  const season = seasonText(anime)
  if (season !== null) parts.push(season)

  if (episodes !== null) parts.push(`${String(episodes)} episode${episodes === 1 ? '' : 's'}`)

  const genres = anime.genres ?? []
  if (genres.length > 0) parts.push(genres.slice(0, TOP_GENRE_COUNT).join(', '))

  return parts.join(' · ')
}

/**
 * A 36px glass circle in the action row, not an overlay on the artwork.
 *
 * They started inside the frame, where the left one landed squarely on top of
 * the eyebrow and the title and the right one floated over the art (owner, in
 * Chrome, 2026-09-11). Nothing in this design sits on artwork except the
 * hero's own title block, so the whole control — ‹ dots › — moved down beside
 * the buttons, which is also where a thumb already is.
 */
const CHEVRON =
  'flex h-9 w-9 shrink-0 items-center justify-center rounded-full border-[0.5px] border-[var(--arc-border-strong)] bg-[var(--arc-surface-raised)] text-[18px] leading-none text-[var(--arc-text)] backdrop-blur-glass transition-colors duration-200 hover:bg-[rgba(255,255,255,0.13)]'

/**
 * The framed card at the top of Watch Now: shows of the season this viewer
 * might not have found, one at a time.
 *
 * It advances itself every eight seconds and stops the moment a pointer or the
 * keyboard is anywhere inside it — a card that moves out from under the button
 * you were about to press is worse than one that never moved. Under
 * `prefers-reduced-motion` it never starts: the chevrons and dots are then the
 * only way through, which is the whole carousel, just hand-driven.
 */
function SeasonHero({ items }: { items: AnimeSummary[] }) {
  const [index, setIndex] = useState(0)
  const [paused, setPaused] = useState(false)
  // What this hero has put on the list in this session. The caches it reads
  // are refetched after a write, and until that lands the button would say
  // "Add to list" over a show that is already planned.
  const [added, setAdded] = useState<number[]>([])
  const reduced = usePrefersReducedMotion()
  const setEntry = useSetListEntry()

  const count = items.length
  const position = count === 0 ? 0 : Math.min(index, count - 1)
  const anime = items[position]

  useEffect(() => {
    if (count < 2 || paused || reduced) return
    const timer = setTimeout(() => {
      setIndex((current) => (current + 1) % count)
    }, HERO_INTERVAL_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [count, paused, reduced, position])

  if (anime === undefined) return null

  const animeId = anime.id
  const onList = anime.list_status !== null || added.includes(animeId)
  const meta = recommendationMeta(anime)
  const { native } = anime.title

  function step(delta: number): void {
    setIndex((((position + delta) % count) + count) % count)
  }

  function add(): void {
    setEntry.mutate(
      { animeId, status: 'planned' },
      {
        onSuccess: () => {
          setAdded((current) => (current.includes(animeId) ? current : [...current, animeId]))
        },
      },
    )
  }

  return (
    <section
      aria-label={HERO_EYEBROW}
      onMouseEnter={() => {
        setPaused(true)
      }}
      onMouseLeave={() => {
        setPaused(false)
      }}
      onFocus={() => {
        setPaused(true)
      }}
      onBlur={() => {
        setPaused(false)
      }}
    >
      <HeroFrame banner={bannerArt(anime)} poster={keyVisual(anime)}>
        <Eyebrow>{HERO_EYEBROW}</Eyebrow>
        <h1 className="mt-2.5 text-[clamp(30px,6vw,46px)] leading-[1.04] font-semibold tracking-[-0.03em] text-pretty text-white">
          {anime.title.preferred}
        </h1>
        {native === null || native === '' ? null : (
          <p className="mt-2 font-jp text-[17px] tracking-[0.02em] text-[rgba(235,239,245,0.78)]">
            {native}
          </p>
        )}
        {meta === '' ? null : (
          <p className="mt-3.5 text-[16px] leading-[1.5] text-[rgba(235,239,245,0.85)]">{meta}</p>
        )}
      </HeroFrame>

      <div className="mt-[22px] flex flex-wrap items-center gap-3.5">
        <Button variant="primary" disabled={onList || setEntry.isPending} onClick={add}>
          {onList ? ADDED_LABEL : ADD_LABEL}
        </Button>
        <Link to={`/anime/${String(animeId)}`} className={buttonClass('secondary')}>
          Details
        </Link>

        {count > 1 ? (
          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              aria-label="Previous recommendation"
              onClick={() => {
                step(-1)
              }}
              className={cx(CHEVRON, FOCUS_RING)}
            >
              <span aria-hidden>‹</span>
            </button>

            <div className="flex items-center">
              {items.map((item, slide) => (
                <button
                  key={item.id}
                  type="button"
                  aria-label={item.title.preferred}
                  aria-current={slide === position ? 'true' : undefined}
                  onClick={() => {
                    setIndex(slide)
                  }}
                  className={cx(
                    'flex h-11 w-6 items-center justify-center rounded-nav',
                    FOCUS_RING,
                  )}
                >
                  <span
                    aria-hidden
                    className={cx(
                      'h-2 w-2 rounded-full transition-colors duration-200',
                      slide === position
                        ? 'bg-[var(--arc-text)]'
                        : 'bg-[rgba(255,255,255,0.3)] hover:bg-[rgba(255,255,255,0.55)]',
                    )}
                  />
                </button>
              ))}
            </div>

            <button
              type="button"
              aria-label="Next recommendation"
              onClick={() => {
                step(1)
              }}
              className={cx(CHEVRON, FOCUS_RING)}
            >
              <span aria-hidden>›</span>
            </button>
          </div>
        ) : null}
      </div>

      {setEntry.error === null ? null : (
        <p role="alert" className="mt-2.5 text-[13px] text-[var(--arc-error)]">
          {listErrorMessage(setEntry.error)}
        </p>
      )}
    </section>
  )
}

/* --- Episode shelves --------------------------------------------------- */

/**
 * One episode the viewer can press play on right now: either one they are part
 * way through, or one that is ready and unstarted. `position` is null for the
 * second kind, which is what the tile reads to decide between "Resume" and
 * "Play" — an unstarted episode has no progress strip and no clock, and
 * inventing a 0 % bar for it would read as "you stopped here".
 */
interface EpisodeItem {
  anime: AnimeSummary
  episode: EpisodeOut
  position: number | null
  duration: number | null
}

/** Whole minutes left, or null when nothing knows how long the episode is. */
function minutesLeft(item: EpisodeItem): number | null {
  const { position, duration } = item
  if (position === null || duration === null || duration <= 0) return null
  return Math.max(1, Math.round(Math.max(0, duration - position) / 60))
}

/** How far in, 0–1, or null when there is no fraction to state. */
function fractionWatched(item: EpisodeItem): number | null {
  const { position, duration } = item
  if (position === null || duration === null || duration <= 0) return null
  return Math.min(1, Math.max(0, position / duration))
}

/** What the viewer started and has not finished, newest first (FR-W1). */
function continueWatching(home: HomePage): EpisodeItem[] {
  return home.continue_watching.slice(0, SHELF_LIMIT).map((entry) => ({
    anime: entry.anime,
    episode: entry.episode,
    position: entry.position_s,
    duration: entry.duration_s,
  }))
}

/**
 * Episodes that arrived this week, are playable, and have not been opened.
 * An episode already on the Continue watching shelf is left out: it is the
 * same episode, and the shelf above it is the one that says what to do.
 */
function readyToWatch(home: HomePage): EpisodeItem[] {
  const started = new Set(home.continue_watching.map((entry) => entry.episode.id))

  return home.new_this_week
    .filter(
      (entry) =>
        entry.episode.state === 'ready' && !entry.episode.watched && !started.has(entry.episode.id),
    )
    .slice(0, SHELF_LIMIT)
    .map((entry) => ({
      anime: entry.anime,
      episode: entry.episode,
      position: null,
      duration: null,
    }))
}

/**
 * "Episode 12 · 9 min left", or the episode alone when there is nothing to add
 * — the Ready shelf's own heading already says the episode is ready, and
 * repeating it on every tile is noise. "In progress" is the honest middle
 * case: started, but nothing ever recorded how long the episode runs.
 */
function tileLine(item: EpisodeItem): string {
  const parts = [`Episode ${String(item.episode.number)}`]
  const left = minutesLeft(item)

  if (left !== null) parts.push(`${String(left)} min left`)
  else if (item.position !== null) parts.push('In progress')

  return parts.join(' · ')
}

/**
 * The 16:9 art for an episode: its own still, else the show's banner, else
 * the key visual. The last of those is 2:3 and will crop, which is still the
 * show's own artwork and beats a striped placeholder on the shelf people are
 * meant to press play from.
 */
function stillUrl(item: EpisodeItem): string | null {
  return item.episode.still_url ?? heroArt(item.anime)
}

function EpisodeTile({ item }: { item: EpisodeItem }) {
  const resuming = item.position !== null

  return (
    <Link
      to={`/watch/${String(item.episode.id)}`}
      aria-label={`${resuming ? 'Resume' : 'Play'} ${item.anime.title.preferred} episode ${String(item.episode.number)}`}
      className={cx('group block w-[280px] max-w-[78vw] rounded-art', FOCUS_RING)}
    >
      <Artwork
        url={stillUrl(item)}
        shape="still"
        progress={fractionWatched(item)}
        className="shadow-tile transition-transform duration-[240ms] ease-arc group-hover:-translate-y-[5px]"
      />
      <p className="mt-2.5 truncate text-[16px] font-medium text-[var(--arc-text)]">
        {item.anime.title.preferred}
      </p>
      <p className="mt-1 truncate text-[14px] text-[var(--arc-text-muted)]">{tileLine(item)}</p>
    </Link>
  )
}

/* --- This week --------------------------------------------------------- */

/** One broadcast the viewer keeps an appointment with (design: "This week"). */
interface Appointment {
  anime: AnimeSummary
  weekday: number
  time: string
  episode: string
  /** What Arc has done about it, in words. */
  state: string
  /** True when the file is here; the line brightens rather than changing hue. */
  ready: boolean
  tonight: boolean
}

/**
 * What Arc will do, or has done, about this week's episode of a show.
 *
 * The home aggregate carries the episodes that actually aired in the last
 * seven days with their state, so a show that already has its file says so.
 * For one still to come, the broadcast time *is* the answer — Arc searches
 * once an episode has aired — and a show with neither gets the honest
 * "waiting", never a guess.
 */
function acquisitionState(
  home: HomePage,
  animeId: number,
  airTime: string | null,
  upcoming: boolean,
): { state: string; ready: boolean } {
  const found = home.new_this_week.find((entry) => entry.anime.id === animeId)
  if (found !== undefined) {
    if (found.episode.state === 'ready') return { state: 'Ready', ready: true }
    return { state: episodeStateLabel(found.episode.state), ready: false }
  }
  if (upcoming && airTime !== null && airTime !== '') {
    return { state: `Arc will search at ${airTime}`, ready: false }
  }
  return { state: 'Waiting for a release', ready: false }
}

/**
 * The week ahead, starting today and wrapping round to yesterday: the shelf is
 * read left to right as "tonight, then tomorrow", so a Thursday viewer should
 * not have to scroll past Monday to find it.
 */
function appointments(schedule: SchedulePage, home: HomePage, today: number): Appointment[] {
  // -1 means the viewer's timezone did not resolve to a weekday; the week then
  // starts on Monday and nothing is "tonight", which is the honest fallback.
  const start = today < 0 ? 0 : today
  const order = WEEKDAY_LABELS.map((_, index) => (start + index) % 7)

  const cards: Appointment[] = []
  for (const weekday of order) {
    const day = schedule.days.find((candidate) => candidate.weekday === weekday)
    if (day === undefined) continue
    for (const entry of day.entries) {
      if (!entry.following) continue
      const upcoming = entry.next_at !== null && Date.parse(entry.next_at) > Date.now()
      cards.push({
        anime: entry.anime,
        weekday,
        time: entry.air_time_local ?? '—',
        episode:
          entry.next_episode === null ? 'Next episode' : `Episode ${String(entry.next_episode)}`,
        tonight: weekday === today,
        ...acquisitionState(home, entry.anime.id, entry.air_time_local, upcoming),
      })
    }
  }
  return cards.slice(0, THIS_WEEK_LIMIT)
}

function AppointmentCard({ card }: { card: Appointment }) {
  return (
    <Link
      to={`/anime/${String(card.anime.id)}`}
      className={cx(
        'block w-[210px] rounded-card border-[0.5px] p-[18px] transition-colors duration-200',
        card.tonight
          ? 'border-[rgba(255,122,41,0.3)] bg-[rgba(255,122,41,0.07)]'
          : 'border-[var(--arc-border)] bg-[var(--arc-surface)] hover:bg-[rgba(255,255,255,0.08)]',
        FOCUS_RING,
      )}
    >
      <div className="flex items-center gap-2">
        {card.tonight ? (
          <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--arc-spark)]" />
        ) : null}
        <Eyebrow tone={card.tonight ? 'ember' : 'muted'} className="tracking-[0.08em]">
          {card.tonight ? 'Tonight' : (WEEKDAY_LABELS[card.weekday] ?? '')}
        </Eyebrow>
      </div>

      <p className="mt-1.5 text-[28px] leading-tight font-semibold tracking-[-0.02em] tabular-nums text-[var(--arc-text)]">
        {card.time}
      </p>

      <div className="mt-3.5 flex items-center gap-2.5">
        <Artwork url={keyVisual(card.anime)} shape="thumb" className="w-[38px] shrink-0" />
        <div className="min-w-0">
          <p className="truncate text-[14px] text-[var(--arc-text)]">
            {card.anime.title.preferred}
          </p>
          <p className="truncate text-[13px] text-[var(--arc-text-muted)]">{card.episode}</p>
        </div>
      </div>

      <p
        className={cx(
          'mt-3 text-[13px]',
          card.ready ? 'text-[var(--arc-text)]' : 'text-[var(--arc-text-muted)]',
        )}
      >
        {card.state}
      </p>
    </Link>
  )
}

/* --- Key-visual shelves ------------------------------------------------ */

/** A 172px 2:3 tile: catch-up shows and recommendation picks. No badge on art. */
function KeyTile({ anime, meta }: { anime: AnimeSummary; meta: string }) {
  return (
    <Link
      to={`/anime/${String(anime.id)}`}
      className={cx('group block w-[172px] rounded-art', FOCUS_RING)}
    >
      <Artwork
        url={keyVisual(anime)}
        shape="key"
        className="shadow-tile transition-transform duration-[240ms] ease-arc group-hover:-translate-y-[5px]"
      />
      <p className="mt-2.5 line-clamp-2 text-[15px] leading-snug font-medium text-[var(--arc-text)]">
        {anime.title.preferred}
      </p>
      {meta === '' ? null : (
        <p className="mt-1 truncate text-[13px] text-[var(--arc-text-muted)]">{meta}</p>
      )}
    </Link>
  )
}

/**
 * "Fall 2023 · 4 episodes behind" — the count is prose and muted on purpose
 * (the design calls it out): being a cour behind is normal, and a red badge on
 * the artwork would turn a shelf of good shows into a list of chores.
 */
function behindMeta(item: BehindEntry): string {
  const parts: string[] = []
  const { season, season_year, format } = item.anime
  // A season the catalogue spells in a way this client does not know is left
  // out rather than printed raw: "FALL 2023" in a shelf of prose reads as a bug.
  if (season !== null && season_year !== null && (SEASONS as readonly string[]).includes(season)) {
    parts.push(seasonLabel(season_year, season as Season))
  } else if (format !== null) {
    parts.push(format)
  }
  parts.push(`${String(item.behind)} episode${item.behind === 1 ? '' : 's'} behind`)
  return parts.join(' · ')
}

/* --- Page -------------------------------------------------------------- */

export function Home() {
  const { data: me } = useMe()
  const home = useHome()
  const schedule = useSchedule()
  const recs = useRecs()
  const myList = useMyList()

  // The hero's line-up is worked out once, from the first render at which all
  // three of its inputs have answered — a set that is rebuilt on every render
  // would drop the show under the cursor the moment it was added to the list,
  // and a set built while `/api/list` was still in flight would be the
  // no-genres fallback rather than this viewer's own.
  const heroReady = !schedule.isPending && !recs.isPending && !myList.isPending
  const candidates = heroReady
    ? seasonRecommendations(schedule.data, recs.data, myList.data ?? [])
    : []
  const [heroOrder, setHeroOrder] = useState<number[] | null>(null)
  if (heroOrder === null && candidates.length > 0) {
    setHeroOrder(candidates.map((anime) => anime.id))
  }
  const pool = heroPool(schedule.data, recs.data)
  const heroItems = (heroOrder ?? [])
    .map((id) => pool.get(id))
    .filter((anime): anime is AnimeSummary => anime !== undefined)

  const data = home.data
  const timezone = me?.timezone ?? ''

  if (home.isError) {
    return (
      <section>
        <h1 className="sr-only">Watch Now</h1>
        <ErrorState
          message={catalogErrorMessage(home.error, HOME_ERROR)}
          pending={home.isFetching}
          onRetry={() => {
            void home.refetch()
          }}
        />
        <Footer />
      </section>
    )
  }

  if (data === undefined) {
    return (
      <section>
        <h1 className="sr-only">Watch Now</h1>
        <Skeleton shape="hero" />
        <Skeleton shape="tile" count={4} label={null} className="mt-[72px]" />
        <Skeleton shape="key" count={6} label={null} className="mt-[72px]" />
        <Footer />
      </section>
    )
  }

  const started = continueWatching(data)
  const ready = readyToWatch(data)
  const week =
    schedule.data === undefined
      ? []
      : appointments(schedule.data, data, weekdayInTimezone(timezone))
  const picks = recs.data?.run?.picks ?? []

  // Built as a list rather than five conditionals in the tree, so the 72px
  // section rhythm is one gap between whatever survived rather than a margin
  // each shelf has to work out for itself.
  const shelves: ReactNode[] = []

  if (started.length > 0) {
    shelves.push(
      <Shelf key="continue" title="Continue watching" lede={CONTINUE_LEDE}>
        {started.map((item) => (
          <EpisodeTile key={item.episode.id} item={item} />
        ))}
      </Shelf>,
    )
  }

  if (ready.length > 0) {
    shelves.push(
      <Shelf key="ready" title="Ready to watch" lede={READY_LEDE}>
        {ready.map((item) => (
          <EpisodeTile key={item.episode.id} item={item} />
        ))}
      </Shelf>,
    )
  }

  if (week.length > 0) {
    shelves.push(
      <Shelf
        key="this-week"
        title="This week"
        lede={THIS_WEEK_LEDE}
        scrollerClassName="gap-4"
        action={
          <Link to="/schedule" className={buttonClass('chip')}>
            Full schedule
          </Link>
        }
      >
        {week.map((card) => (
          <AppointmentCard key={`${String(card.weekday)}:${String(card.anime.id)}`} card={card} />
        ))}
      </Shelf>,
    )
  }

  if (data.behind.length > 0) {
    shelves.push(
      <Shelf key="catch-up" title="Catch up" lede={CATCH_UP_LEDE}>
        {data.behind.map((item) => (
          <KeyTile key={item.anime.id} anime={item.anime} meta={behindMeta(item)} />
        ))}
      </Shelf>,
    )
  }

  if (picks.length > 0) {
    shelves.push(
      <Shelf
        key="picks"
        title="Picked for you"
        lede={PICKS_LEDE}
        action={
          <Link to="/recs" className={buttonClass('chip')}>
            Recommendations
          </Link>
        }
      >
        {picks.map((pick) => (
          <KeyTile key={pick.anime.id} anime={pick.anime} meta={pick.anime.format ?? ''} />
        ))}
      </Shelf>,
    )
  }

  // With no hero the page has to start with something, and a frame's worth of
  // blank space is not it: the sr-only title becomes the visible one.
  const hasHero = heroItems.length > 0

  return (
    <section>
      {hasHero ? (
        <SeasonHero items={heroItems} />
      ) : (
        <h1 className="text-[clamp(26px,4.5vw,34px)] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
          Watch Now
        </h1>
      )}

      {shelves.length === 0 && !hasHero ? (
        <EmptyState
          className="mt-7"
          title={EMPTY_TITLE}
          message={EMPTY_MESSAGE}
          action={
            <Link to="/search" className={buttonClass('primary')}>
              Browse the catalogue
            </Link>
          }
        />
      ) : null}

      {shelves.length === 0 ? null : (
        <div className={cx('flex flex-col gap-[72px]', hasHero ? 'mt-[72px]' : 'mt-9')}>
          {shelves}
        </div>
      )}

      <Footer />
    </section>
  )
}

/** The API's own state, kept from the pre-M15 page: quiet, and last. */
function Footer() {
  return (
    <p className="mt-[72px] text-[13px] text-[var(--arc-text-muted)]">
      <HealthBadge />
    </p>
  )
}
