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

/** `Intl`'s short weekday names in the same Monday-first order. */
const WEEKDAY_CODES: readonly string[] = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

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

/** An episode that aired in the last seven days for a followed show. */
export interface NewEpisodeEntry {
  anime: AnimeSummary
  episode: EpisodeOut
}

/** `GET /api/home` — the three FR-W1 sections. */
export interface HomePage {
  /** Empty until playback lands (roadmap M8); shape is not fixed yet. */
  continue_watching: unknown[]
  behind: BehindEntry[]
  new_this_week: NewEpisodeEntry[]
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
