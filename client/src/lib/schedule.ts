/**
 * Seasonal schedule and home dashboard data layer (spec §4.1 FR-C3/FR-C4,
 * §4.6 FR-W1, roadmap M4).
 *
 * Both endpoints are server-side aggregates: the weekday grouping, the air
 * times and the "behind by N" arithmetic all happen there, in the viewer's own
 * timezone, so the client renders what it is given rather than re-deriving it.
 * The one thing the client still has to work out for itself is which column is
 * *today* — that depends on the moment the page is looked at, not on the
 * response — hence `weekdayInTimezone`.
 *
 * `anime.ts` imports the query-key constants and `isFollowing` from here to
 * patch and then invalidate both caches after a list write: putting a show on
 * the list changes its highlight on the schedule and can add or remove a
 * "behind on" card.
 */

import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'
import type { AnimeSummary, EpisodeOut, ListEntry, ListStatus } from '@/lib/anime'

/** AniList's four seasons, as the server spells them. */
export type Season = 'WINTER' | 'SPRING' | 'SUMMER' | 'FALL'

export const SEASONS: readonly Season[] = ['WINTER', 'SPRING', 'SUMMER', 'FALL']

export const SEASON_LABELS: Record<Season, string> = {
  WINTER: 'Winter',
  SPRING: 'Spring',
  SUMMER: 'Summer',
  FALL: 'Fall',
}

/**
 * Monday-first, matching `ScheduleDay.weekday` (0 = Monday). The server has
 * already shifted each show into the viewer's timezone, so a title that airs
 * Tuesday 02:00 in Tokyo lands in the Monday column for a European viewer.
 */
export const WEEKDAY_LABELS: readonly string[] = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
]

/**
 * `Intl`'s short weekday names in the same Monday-first order. Exported
 * because the three-day window's bar names each day short ("Wed 17 Sep"): the
 * full name is the column's accessible name, the code is what is drawn.
 */
export const WEEKDAY_CODES: readonly string[] = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/**
 * The statuses the server counts as *following* a show: the ones a person
 * still intends to watch. `dropped` and `completed` are on the list but are
 * not followed, so they neither light up a schedule row nor earn a "behind on"
 * card (FR-C3, FR-C4).
 */
export const FOLLOWING_STATUSES: readonly ListStatus[] = ['watching', 'planned', 'on_hold']

export function isFollowing(status: ListStatus | null): boolean {
  return status !== null && FOLLOWING_STATUSES.includes(status)
}

/** Where prev/next point; the season either side of the one being shown. */
export interface SeasonRef {
  year: number
  season: Season
}

export interface ScheduleEntry {
  anime: AnimeSummary
  /** "HH:MM" in the viewer's timezone; null when the slot is unknown. */
  air_time_local: string | null
  next_episode: number | null
  next_at: string | null
  /**
   * True when `next_at` was synthesised from a MAL broadcast slot rather than
   * given by AniList as a real airing time (FR-C6); false whenever `next_at`
   * is null. Rendered as the same "est." marker the show page uses.
   */
  next_at_estimated: boolean
  /** True when the show is on the viewer's list in a following status. */
  following: boolean
  list_status: ListStatus | null
  /**
   * True when the show is in this week because it is on air, not because it
   * carries the season being shown: a two-cour show that started last season,
   * or a long-runner the catalogue tags with no season at all. Only the
   * *current* season's grid takes such rows — a prev/next view is a catalogue
   * browse and stays "the shows of that season" — so this is always false
   * there. The page turns it into a "Since Spring 2026" line, read off
   * `anime.season`/`anime.season_year`.
   */
  carried_over: boolean
  /**
   * Whether the viewer has watched the episode *this slot names* (FR-W5), or
   * null when there is nothing to say: the slot names no episode, or the
   * episode it names has not aired yet. An appointment in the future never
   * carries a tick — Watch Now used to draw one beside Friday's broadcast,
   * having read it off the show's latest aired episode rather than the one on
   * the card (owner, 2026-09-17).
   */
  watched: boolean | null
}

export interface ScheduleDay {
  /** 0 = Monday … 6 = Sunday, already in the viewer's timezone. */
  weekday: number
  /** Sorted by air time; the client keeps the server's order. */
  entries: ScheduleEntry[]
}

/** `GET /api/schedule` — one season's grid plus the seasons either side. */
export interface SchedulePage {
  year: number
  season: Season
  prev: SeasonRef
  next: SeasonRef
  /** The IANA zone every time on this page is expressed in. */
  timezone: string
  /** Always seven, Monday first. */
  days: ScheduleDay[]
  /** Movies, OVAs and anything with no weekday slot. */
  unscheduled: ScheduleEntry[]
}

/** One followed show with aired episodes the viewer has not watched (FR-C4). */
export interface BehindEntry {
  anime: AnimeSummary
  entry: ListEntry
  /** Episodes that have aired so far. */
  aired: number
  /** `aired` minus the viewer's progress. */
  behind: number
  latest_aired_at: string | null
}

/**
 * One episode of a followed show, as an episode shelf draws it. Both 16:9
 * shelves the server decides — `new_this_week` and `ready_to_watch` — are
 * lists of these.
 */
export interface NewEpisodeEntry {
  anime: AnimeSummary
  episode: EpisodeOut
}

/**
 * An episode the viewer started and has not finished (FR-W1). Both times are
 * seconds: `position_s` is where they stopped, `duration_s` the length the
 * transcode reported, so the card can draw the bar without asking the player.
 *
 * `duration_s` is null when nothing has recorded a length yet — the column is
 * nullable server-side, and a report can land before the rendition's duration
 * is known. A card with no duration can still say where the viewer got to; it
 * just cannot say how far through that is.
 */
export interface ContinueWatchingEntry {
  anime: AnimeSummary
  episode: EpisodeOut
  position_s: number
  duration_s: number | null
}

/** Which of the two questions produced a failure row (FR-W6). */
export type FailureKind = 'episode' | 'mal'

/**
 * One of the viewer's **own** failures, as the banner above the hero draws it
 * (FR-W6, M16). One flat shape for both kinds, because the banner is one strip
 * in one order; `kind` says which half is filled.
 *
 * `key` is what a dismissal is remembered under, and the server guarantees the
 * two properties that makes it usable for that: the same failure carries the
 * same key on the next request, and a *new* failure on the same episode —
 * FR-A6 retries daily — carries a different one, so putting one away today
 * cannot hide tomorrow's.
 */
export interface FailureEntry {
  key: string
  kind: FailureKind
  anime: AnimeSummary
  /** One sentence, already trimmed server-side. Never a stderr wall. */
  reason: string
  /** When it happened, or null where nothing dated it. */
  since: string | null
  /** The episode that stopped (`kind: 'episode'`). */
  episode_id: number | null
  episode_number: number | null
  /** `failed` — a transcode broke — or `unavailable` — no release found. */
  state: string | null
  /** The write that did not land (`kind: 'mal'`). */
  log_id: number | null
  field: string | null
  old_value: unknown
  new_value: unknown
}

/** `GET /api/home` — the FR-W1 shelves. */
export interface HomePage {
  /** Most recent first; the server caps the list, the client caps it again. */
  continue_watching: ContinueWatchingEntry[]
  /**
   * Ready, unstarted and unwatched, newest file first — whenever the episode
   * aired (owner, 2026-09-17). The server decides the whole shelf: the client
   * used to filter `new_this_week` for it, which silently required the episode
   * to have been broadcast in the last seven days and so hid every ready
   * episode of an older show.
   */
  ready_to_watch: NewEpisodeEntry[]
  behind: BehindEntry[]
  new_this_week: NewEpisodeEntry[]
  /**
   * The viewer's own broken episodes and their own MyAnimeList writes that did
   * not land (FR-W6, M16), newest first. On this payload rather than an
   * endpoint of its own so the live-update invalidation of the home query
   * carries it (architecture.md §5.9). Optional on the wire so a response
   * cached before M16 still parses; empty is the ordinary answer.
   */
  failures?: FailureEntry[]
}

export const SCHEDULE_QUERY_KEY = 'schedule'
export const HOME_QUERY_KEY = 'home'

export function scheduleQueryKey(
  year?: number,
  season?: Season,
): readonly [string, number | undefined, Season | undefined] {
  return [SCHEDULE_QUERY_KEY, year, season]
}

export const homeQueryKey = [HOME_QUERY_KEY] as const

/** "Fall 2026". */
export function seasonLabel(year: number, season: Season): string {
  return `${SEASON_LABELS[season]} ${String(year)}`
}

/** Narrows a raw URL parameter to a season the server will accept. */
export function parseSeason(value: string | null): Season | undefined {
  if (value === null) return undefined
  const upper = value.toUpperCase()
  return (SEASONS as readonly string[]).includes(upper) ? (upper as Season) : undefined
}

/** A four-digit year from a raw URL parameter, or undefined if it is junk. */
export function parseYear(value: string | null): number | undefined {
  if (value === null || !/^\d{4}$/.test(value)) return undefined
  return Number(value)
}

/**
 * The season Arc is in, by the same arithmetic the server uses
 * (`services/catalog/seasons.py`): three calendar months each, Jan–Mar
 * `WINTER` … Oct–Dec `FALL`, measured in **UTC** rather than the viewer's zone
 * so that both ends agree about which grid is the live one.
 *
 * The response does not say whether the season on screen is the current one —
 * it says which season it *is* — and the page has to know, because only the
 * current grid is a real week: it is the one the server fills with every show
 * on air, and the only one whose columns have dates and a today (owner,
 * 2026-09-17). A prev/next view is a catalogue browse, and printing this
 * week's dates over Spring 2026's shows would be a claim about when they air.
 */
export function currentSeason(at: Date = new Date()): SeasonRef {
  return {
    year: at.getUTCFullYear(),
    season: SEASONS[Math.floor(at.getUTCMonth() / 3)] ?? 'WINTER',
  }
}

/** Whether the grid on screen is the live week rather than a browse. */
export function isCurrentSeason(page: SeasonRef, at: Date = new Date()): boolean {
  const now = currentSeason(at)
  return page.year === now.year && page.season === now.season
}

/**
 * Which column is today, as a `ScheduleDay.weekday` (0 = Monday).
 *
 * The grid is in the viewer's timezone, so "today" has to be read in that same
 * zone: at 23:00 UTC on a Monday it is already Tuesday in Tokyo, and the Tokyo
 * viewer should see Tuesday lit up. A zone the browser rejects falls back to
 * the browser's own rather than blanking the highlight.
 *
 * Returns -1 — no column — when `Intl` answers with a weekday code this
 * doesn't recognise. Lighting up Monday on a guess is worse than lighting up
 * nothing: the grid is read for "what airs today", and a wrong today misleads.
 */
export function weekdayInTimezone(timezone: string, at: Date = new Date()): number {
  const short = shortWeekday(timezone === '' ? undefined : timezone, at)
  return WEEKDAY_CODES.indexOf(short)
}

function shortWeekday(timezone: string | undefined, at: Date): string {
  try {
    return new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: timezone }).format(at)
  } catch {
    return new Intl.DateTimeFormat('en-US', { weekday: 'short' }).format(at)
  }
}

const DAY_MS = 86_400_000

/**
 * The month of "17 Sep" — day and month, never the year, because the grid is
 * one week long. Composed rather than formatted whole: a locale decides both
 * the order ("Sep 17") and the abbreviation, and `en-GB` spells this month
 * "Sept", which is a character wider than every other month in a heading that
 * has to line up three times across.
 */
const MONTH_SHORT = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' })

/**
 * The dates of the Monday–Sunday week that `at` falls in, as "17 Sep" labels
 * indexed the way `ScheduleDay.weekday` is (0 = Monday).
 *
 * The three-day window (owner, 2026-09-17) names its columns "Wed 17 Sep", and
 * a weekday alone cannot say which Wednesday. The server groups the week in
 * the viewer's timezone, so the dates have to be read in that zone too — at
 * 23:00 UTC on a Sunday the Tokyo viewer is already in the *next* week, and
 * his Monday column is tomorrow rather than six days ago.
 *
 * Arithmetic happens at noon UTC: adding 24h to a local midnight lands on the
 * same day again in a zone that put its clocks back that night, and no zone
 * shifts a noon across a date boundary.
 *
 * Returns an empty array when the zone cannot be read at all — the bar then
 * shows weekday names alone, which is what it showed before there were dates.
 */
export function weekDates(timezone: string, at: Date = new Date()): string[] {
  const weekday = weekdayInTimezone(timezone, at)
  const today = dateInTimezone(timezone === '' ? undefined : timezone, at)
  if (weekday < 0 || today === null) return []

  const monday = Date.UTC(today.year, today.month - 1, today.day - weekday, 12)
  return WEEKDAY_CODES.map((_, index) => {
    const day = new Date(monday + index * DAY_MS)
    return `${String(day.getUTCDate())} ${MONTH_SHORT.format(day)}`
  })
}

interface CalendarDate {
  year: number
  month: number
  day: number
}

/** The civil date `at` falls on in `timezone`, read off `Intl`'s own parts. */
function dateInTimezone(timezone: string | undefined, at: Date): CalendarDate | null {
  const parts = dateParts(timezone, at)
  const year = Number(parts.year)
  const month = Number(parts.month)
  const day = Number(parts.day)
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return null
  return { year, month, day }
}

function dateParts(timezone: string | undefined, at: Date): Record<string, string> {
  const options: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  }
  let formatted: Intl.DateTimeFormatPart[]
  try {
    formatted = new Intl.DateTimeFormat('en-US', { ...options, timeZone: timezone }).formatToParts(
      at,
    )
  } catch {
    formatted = new Intl.DateTimeFormat('en-US', options).formatToParts(at)
  }
  return Object.fromEntries(formatted.map((part) => [part.type, part.value]))
}

/**
 * One season's grid. Both parameters or neither: the server answers with the
 * current season when they are absent, which is what the bare `/schedule` URL
 * means. Previous data is kept while stepping seasons so the grid dims rather
 * than emptying.
 */
export function useSchedule(year?: number, season?: Season): UseQueryResult<SchedulePage, Error> {
  return useQuery<SchedulePage, Error>({
    queryKey: scheduleQueryKey(year, season),
    queryFn: () => {
      const params = new URLSearchParams()
      if (year !== undefined) params.set('year', String(year))
      if (season !== undefined) params.set('season', season)
      const query = params.toString()
      return apiFetch<SchedulePage>(`/api/schedule${query === '' ? '' : `?${query}`}`)
    },
    placeholderData: keepPreviousData,
    // A season the server could not build does not build on a second ask.
    retry: false,
  })
}

export function useHome(): UseQueryResult<HomePage, Error> {
  return useQuery<HomePage, Error>({
    queryKey: homeQueryKey,
    queryFn: () => apiFetch<HomePage>('/api/home'),
    retry: false,
  })
}
