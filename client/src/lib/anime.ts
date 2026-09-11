/**
 * Catalogue and list-state data layer (spec §4.1 FR-C1/FR-C2, §4.6 FR-W2,
 * roadmap M3).
 *
 * Search results, the show page, the schedule, the home dashboard and "my
 * list" all describe the same anime, and all of them carry the viewer's list
 * status. Setting a status therefore has to land in every cache that shows it:
 * the mutation patches each one directly, so a select never snaps back to its
 * old value while the correcting refetch is in flight, and then invalidates
 * them so the server's own arithmetic — `following`, "behind by N" — replaces
 * the patch a moment later.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { ApiError, apiFetch } from '@/lib/api'
import { errorDetail } from '@/lib/auth'
// Type-only, and erased under `verbatimModuleSyntax`: `mal.ts` imports values
// from here, so this must never become a runtime import.
import type { MalSync } from '@/lib/mal'
// `recs.ts` only imports types from here, so this direction is the runtime one.
import { RECS_QUERY_KEY, type RecsPage } from '@/lib/recs'
import {
  HOME_QUERY_KEY,
  isFollowing,
  SCHEDULE_QUERY_KEY,
  type HomePage,
  type ScheduleEntry,
  type SchedulePage,
} from '@/lib/schedule'

export type ListStatus = 'watching' | 'planned' | 'on_hold' | 'dropped' | 'completed'

/** Order shown in every status control. */
export const LIST_STATUSES: readonly ListStatus[] = [
  'watching',
  'planned',
  'on_hold',
  'dropped',
  'completed',
]

export const LIST_STATUS_LABELS: Record<ListStatus, string> = {
  watching: 'Watching',
  planned: 'Plan to watch',
  on_hold: 'On hold',
  dropped: 'Dropped',
  completed: 'Completed',
}

/** Narrows a wire string — a select's value, a MAL log entry — to a status. */
export function isListStatus(value: string): value is ListStatus {
  return (LIST_STATUSES as readonly string[]).includes(value)
}

export interface AnimeTitle {
  romaji: string | null
  english: string | null
  native: string | null
  /** English if there is one, else romaji: the server never sends this null. */
  preferred: string
}

/** Which catalogue a record was last filled from (spec §4.1 FR-C6). */
export type CatalogSource = 'anilist' | 'mal'

export interface AnimeSummary {
  /**
   * Arc's own id (FR-C6). Opaque to the client — it is what routes and every
   * `/api/anime/*` and `/api/list/*` call use. The catalogue ids below are for
   * display and outbound links only; never call the API with one.
   */
  id: number
  title: AnimeTitle
  format: string | null
  /** Total episode count; null while a show airs without a known count. */
  episodes: number | null
  /** AniList's airing status ("FINISHED", "RELEASING", …); null if unknown. */
  status: string | null
  season: string | null
  season_year: number | null
  cover_url: string | null
  /**
   * The key visual at AniList's largest size (M15). Optional on the wire: a
   * record filled from MAL has only the small cover, and fixtures written
   * before this pass omit it — so every reader goes through `keyVisual`,
   * which falls back to `cover_url` rather than rendering a missing image.
   */
  cover_large_url?: string | null
  /**
   * The 21:9 banner (M15), or null — the ordinary case for a record filled
   * from MAL, which publishes none. Optional on the wire like the two fields
   * below it; a hero falls back to the key visual rather than to nothing.
   */
  banner_url?: string | null
  /**
   * AniList's genres for this show (M15), in its order. Optional and possibly
   * empty: it is what Browse's chips are built from, and a result that
   * carries none simply matches no chip.
   */
  genres?: string[]
  /**
   * The animation studio (M15), or null. Optional on the wire; a row without
   * one falls back to format and year, never to a blank after a separator.
   */
  studio?: string | null
  /**
   * How many people have the show on a list (AniList `popularity`, MAL
   * `num_list_users`), or null. Unlike the three fields above it is a real
   * summary column — it is in AniList's search fragment — so a card carries it
   * as soon as a search returns it.
   *
   * It is a ranking key, never a number to render: "47,318 people" is a
   * different claim from "popular", and Arc makes neither. Watch Now's hero
   * sorts the season by it (`rankSeason`).
   */
  popularity?: number | null
  /**
   * The mean score on AniList's 0–100 scale — MAL's 0–10 is scaled on the way
   * in, so one number means one thing whichever source answered — or null.
   *
   * Null on a source that publishes none and on an unaired show nobody has
   * rated, which is why it is never rendered as "0 %".
   */
  average_score?: number | null
  /** AniList's id, once the show has been seen there. */
  anilist_id: number | null
  /** MyAnimeList's id, once the show has been seen there. */
  mal_id: number | null
  /**
   * The catalogue that last filled this record. `mal` means AniList was
   * unavailable, so air dates are estimates until it comes back (FR-C6).
   */
  source: CatalogSource | null
  /** The viewer's status for this show, or null when it is not on their list. */
  list_status: ListStatus | null
}

export interface AnimeSearchResponse {
  results: AnimeSummary[]
  page: number
  has_next: boolean
}

/**
 * The Nyaa release Arc picked for an episode, once the ranked rules have
 * chosen one (spec §4.2 FR-A3). `title` is the raw release name and is always
 * set; everything the filename parser could not pull out of it is null.
 */
export interface EpisodeRelease {
  group: string | null
  resolution: string | null
  title: string
  seeders: number | null
}

/**
 * What the transcode actually produced, once an episode is `ready` (spec §4.4
 * FR-P3). `duration` is in seconds; the languages are the tracks that were
 * kept, and are null when the source had none to keep.
 */
export interface EpisodeRendition {
  duration: number
  width: number
  height: number
  subtitle_lang: string | null
  audio_lang: string | null
}

/** Arc's own per-episode state machine (spec §6). */
export interface EpisodeOut {
  id: number
  number: number
  title: string | null
  /**
   * The 16:9 episode thumbnail (M15), or null — the ordinary case for an
   * episode that has not aired and for any show whose detail came from MAL.
   * Optional on the wire for the same reason `cover_large_url` is.
   */
  still_url?: string | null
  air_at: string | null
  /**
   * True when `air_at` was synthesised from a MAL broadcast slot because
   * AniList was unavailable (FR-C6); AniList data replaces it later.
   */
  air_at_estimated: boolean
  aired: boolean
  state: string
  watched: boolean
  /**
   * How far the transfer has got, 0–1, while the episode is `downloading` or
   * `downloaded`; null in every other state (FR-A7).
   */
  download_progress: number | null
  /**
   * How far the transcode has got, 0–1, while the episode is `preparing`;
   * null before ffmpeg has reported anything and in every other state (FR-P4).
   */
  prepare_progress: number | null
  /** Why the transcode broke — the tail of ffmpeg's own error; set only for `failed` (FR-P4). */
  failure_reason: string | null
  /** Why the retry window closed without a release; set only for `unavailable` (FR-A6). */
  unavailable_reason: string | null
  /** The chosen release, once there is one (FR-A3). */
  release: EpisodeRelease | null
  /** What the transcode produced; sent once the episode is `ready` (FR-P3). */
  rendition: EpisodeRendition | null
}

/**
 * A sequel / prequel / side story of the show being viewed.
 *
 * `id` is Arc's own id and is **null** until Arc has a row for the related
 * show — which is the common case, since a relation is a title nobody has
 * necessarily opened yet. Only a relation with an id can be linked to; the
 * external ids are carried for display and for a future "add this".
 */
export interface AnimeRelation {
  id: number | null
  anilist_id: number | null
  mal_id: number | null
  relation_type: string
  title: AnimeTitle
  format: string | null
  /**
   * Arc's own id for the related show, when the catalogue has it cached
   * (M15). The same thing `id` names, under the name the server settled on;
   * both are null for a title nothing has pulled in yet, and either one being
   * present is what makes the card a link.
   */
  anime_id?: number | null
  /**
   * Enough of the related show to draw a card with (M15): its artwork and the
   * three facts under it. All optional and all null when the show is not
   * cached locally — a franchise rail of placeholders is the honest answer,
   * not a reason to hide the rail.
   */
  cover_url?: string | null
  cover_large_url?: string | null
  episodes?: number | null
  season_year?: number | null
}

export interface NextAiring {
  episode: number
  at: string
}

/** The viewer's state for one show; `anime_id` is always sent. */
export interface ListEntry {
  anime_id: number
  status: ListStatus
  progress: number
  score: number | null
  updated_at: string
  /**
   * How this entry stands with MyAnimeList (spec §4.7 FR-M6). Optional
   * because only the show page's `list_entry` carries it — the aggregates
   * (`/api/list`, home, schedule) answer a different question and leave it
   * out, and a badge that is absent must read as "nothing to say", not as
   * "synced".
   */
  mal_sync?: MalSync
}

export interface MyListItem {
  anime: AnimeSummary
  entry: ListEntry
}

/**
 * `GET /api/anime/{id}`. On a card `episodes` is AniList's total count; on the
 * show page it is the per-episode rows, and the count comes back as
 * `episode_count`.
 */
/** One row of the show page's "Made by" block (M15), studio first. */
export interface AnimeCredit {
  role: string
  name: string
}

export interface AnimeDetail extends Omit<AnimeSummary, 'episodes'> {
  episodes: EpisodeOut[]
  episode_count: number | null
  synopsis: string | null
  genres: string[]
  studio: string | null
  /**
   * Studio, director, composer and the rest, in the order the server sends
   * them. Optional and often short — MAL publishes no staff — so a reader
   * renders what arrives rather than expecting six rows.
   */
  credits?: AnimeCredit[]
  banner_url: string | null
  next_airing: NextAiring | null
  relations: AnimeRelation[]
  list_entry: ListEntry | null
}

export interface SetListEntryInput {
  animeId: number
  status?: ListStatus
  progress?: number
  score?: number | null
}

export const ANIME_QUERY_KEY = 'anime'
export const LIST_QUERY_KEY = 'list'

export function animeQueryKey(id: number): readonly [string, number] {
  return [ANIME_QUERY_KEY, id]
}

export function animeSearchQueryKey(
  q: string,
  page: number,
): readonly [string, string, string, number] {
  return [ANIME_QUERY_KEY, 'search', q, page]
}

export function listQueryKey(status?: ListStatus): readonly [string, string] {
  return [LIST_QUERY_KEY, status ?? 'all']
}

/**
 * "TV · 28 eps · 2023" — the muted line under a title, wherever a show is
 * shown as a card. Parts the catalogue does not know are dropped rather than
 * left blank, so a sparse record gets a short line or none at all.
 */
export function summaryLine(anime: AnimeSummary): string {
  const parts: string[] = []
  if (anime.format !== null) parts.push(anime.format)
  if (anime.episodes !== null) parts.push(`${String(anime.episodes)} eps`)
  if (anime.season_year !== null) parts.push(String(anime.season_year))
  return parts.join(' · ')
}

/**
 * The biggest key visual the catalogue has for a show (M15).
 *
 * AniList's large cover where there is one, the small one otherwise: a row
 * filled from MAL carries only a 230 px picture, which is soft at card size
 * but is still the show's own artwork, and the placeholder is worse.
 */
export function keyVisual(
  anime: Pick<AnimeSummary, 'cover_url' | 'cover_large_url'>,
): string | null {
  return anime.cover_large_url ?? anime.cover_url
}

/**
 * AniList's 21:9 banner, or null — never a poster in its place.
 *
 * The one piece of art wide enough to fill a hero frame at its own size: the
 * banner is around 1900 px across, where `cover_url` is a 230–425 px picture
 * that only looks like artwork until something stretches it. A record filled
 * from MAL publishes no banner at all, which is what the poster hero in
 * `HeroFrame` exists for (owner, 2026-09-11).
 */
export function bannerArt(anime: Pick<AnimeSummary, 'banner_url'>): string | null {
  const banner = anime.banner_url
  return banner === null || banner === undefined || banner === '' ? null : banner
}

/** True when a show has art a 21:9 frame can be filled with honestly. */
export function hasBanner(anime: Pick<AnimeSummary, 'banner_url'>): boolean {
  return bannerArt(anime) !== null
}

/**
 * The widest art a show has, for a frame that is wider than it is tall but is
 * not a hero: the banner where AniList has one, then the key visual, which is
 * 2:3 and will crop.
 *
 * Used for an episode still that has none of its own, where a cropped cover is
 * still the show's own artwork and beats the striped placeholder on a shelf
 * people press play from. Not for a hero: see `HeroFrame`.
 */
export function heroArt(
  anime: Pick<AnimeSummary, 'cover_url' | 'cover_large_url' | 'banner_url'>,
): string | null {
  return bannerArt(anime) ?? keyVisual(anime)
}

/** Below this a title match is meaningless and the server answers 422. */
export const MIN_SEARCH_LENGTH = 2

export function isSearchable(q: string): boolean {
  return q.trim().length >= MIN_SEARCH_LENGTH
}

/** A message a person can act on when a list write fails. */
export function listErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not reach the server.'
  switch (error.status) {
    case 404:
      // DELETE /api/list/{id} answers 404 when there is no entry to remove.
      return 'Not on your list.'
    case 422:
      return errorDetail(error) ?? 'That change was rejected.'
    case 429:
      return 'Too many changes at once — try again shortly.'
    default:
      return 'Could not save that change. Try again.'
  }
}

/**
 * The server answers 502 only when *both* catalogue sources are down (FR-C6),
 * so it is worth saying so: it is an outage to wait out, not a query to retype.
 */
export const CATALOGUE_UNAVAILABLE_MESSAGE =
  'The catalogue is unavailable right now. Try again in a few minutes.'

export function catalogErrorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.status === 502
    ? CATALOGUE_UNAVAILABLE_MESSAGE
    : fallback
}

/** Public catalogue pages for a show, for the ids the server knows (FR-C6). */
export function anilistUrl(anilistId: number): string {
  return `https://anilist.co/anime/${String(anilistId)}`
}

export function malUrl(malId: number): string {
  return `https://myanimelist.net/anime/${String(malId)}`
}

const EPISODE_STATE_LABELS: Record<string, string> = {
  not_wanted: 'Not wanted',
  wanted: 'Wanted',
  searching: 'Searching',
  downloading: 'Downloading',
  downloaded: 'Downloaded',
  matching: 'Matching',
  review: 'Review',
  matched: 'Matched',
  preparing: 'Preparing',
  ready: 'Ready',
  failed: 'Failed',
  unavailable: 'Unavailable',
  deleted: 'Deleted',
}

/** Unknown states are shown verbatim rather than hidden. */
export function episodeStateLabel(state: string): string {
  return EPISODE_STATE_LABELS[state] ?? state.replace(/_/g, ' ')
}

const READY = 'text-[var(--arc-ok)] border-[var(--arc-ok)]/40 bg-[var(--arc-ok)]/10'
const BUSY = 'text-[var(--arc-focus)] border-[var(--arc-focus)]/40 bg-[var(--arc-focus)]/10'
const BAD = 'text-[var(--arc-error)] border-[var(--arc-error)]/40 bg-[var(--arc-error)]/10'
const ATTENTION = 'text-[var(--arc-warn)] border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10'
const MUTED =
  'text-[var(--arc-text-muted)] border-[var(--arc-border)] bg-[var(--arc-surface-raised)]'

const EPISODE_STATE_CLASSES: Record<string, string> = {
  ready: READY,
  matched: READY,
  preparing: BUSY,
  downloading: BUSY,
  searching: BUSY,
  downloaded: BUSY,
  matching: BUSY,
  wanted: BUSY,
  failed: BAD,
  unavailable: BAD,
  review: ATTENTION,
  not_wanted: MUTED,
  deleted: MUTED,
}

/** Tailwind classes for a state badge; anything unknown reads as muted. */
export function episodeStateClass(state: string): string {
  return EPISODE_STATE_CLASSES[state] ?? MUTED
}

/**
 * The states in which the server is still working towards a playable file
 * (spec §6). While an episode sits in one of them the answer will change
 * without the viewer touching anything, which is what makes polling worth it.
 */
export const ACTIVE_EPISODE_STATES: readonly string[] = [
  'wanted',
  'searching',
  'downloading',
  'downloaded',
  'matching',
  'preparing',
]

/**
 * How often the show page re-asks while acquisition is in flight (FR-A7).
 * Slow enough to be cheap, fast enough that a progress bar looks alive.
 */
export const ACQUISITION_POLL_MS = 15_000

export function isEpisodeActive(state: string): boolean {
  return ACTIVE_EPISODE_STATES.includes(state)
}

/** True while at least one episode of a show is still on its way to `ready`. */
export function hasActiveEpisode(anime: AnimeDetail | undefined): boolean {
  return anime !== undefined && anime.episodes.some((episode) => isEpisodeActive(episode.state))
}

function clampFraction(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

/**
 * How complete the work on an episode is, 0–1, or null when there is no bar to
 * draw. `downloaded` and `matching` are past the transfer, so they read full
 * whether or not the server still sends a number for them; a `downloading`
 * episode qBittorrent has not reported on yet — or a `preparing` one ffmpeg
 * has not — gets the badge alone rather than a bar stuck at zero.
 */
export function episodeProgress(episode: EpisodeOut): number | null {
  switch (episode.state) {
    case 'downloading':
      return episode.download_progress === null ? null : clampFraction(episode.download_progress)
    case 'downloaded':
    case 'matching':
      return 1
    case 'preparing':
      // The transcode is its own piece of work with its own percentage
      // (FR-P4), but it is the same bar: from the viewer's side one job runs
      // from "wanted" to "ready".
      return episode.prepare_progress === null ? null : clampFraction(episode.prepare_progress)
    default:
      return null
  }
}

/** `episodeProgress` as whole percent, for the bar and the text beside it. */
export function episodeProgressPercent(episode: EpisodeOut): number | null {
  const progress = episodeProgress(episode)
  return progress === null ? null : Math.round(progress * 100)
}

/** The reason an episode will not arrive, shown only where it applies (FR-A6). */
export function unavailableReason(episode: EpisodeOut): string | null {
  if (episode.state !== 'unavailable') return null
  const reason = episode.unavailable_reason
  return reason === null || reason === '' ? null : reason
}

/** The reason a transcode broke, shown only where it applies (FR-P4). */
export function failureReason(episode: EpisodeOut): string | null {
  if (episode.state !== 'failed') return null
  const reason = episode.failure_reason
  return reason === null || reason === '' ? null : reason
}

/**
 * A state that ended badly, with the word the badge already shows and the
 * explanation behind it. The two cases read the same to a viewer — the episode
 * is not coming and here is why — so they get the same affordance, and the
 * label keeps the note from saying "Failed" over an "Unavailable" badge.
 */
export interface EpisodeProblem {
  label: string
  reason: string
}

export function episodeProblem(episode: EpisodeOut): EpisodeProblem | null {
  const unavailable = unavailableReason(episode)
  if (unavailable !== null) return { label: 'Unavailable', reason: unavailable }
  const failure = failureReason(episode)
  return failure === null ? null : { label: 'Failed', reason: failure }
}

/**
 * `[SubsPlease] · 1080p · 123 seeders` — whichever of the three the server
 * knows, in that order. A release the parser got nothing out of falls back to
 * its raw name, which beats an empty line.
 */
export function releaseLine(release: EpisodeRelease): string {
  const parts: string[] = []
  if (release.group !== null && release.group !== '') parts.push(`[${release.group}]`)
  if (release.resolution !== null && release.resolution !== '') parts.push(release.resolution)
  if (release.seeders !== null) {
    parts.push(`${String(release.seeders)} seeder${release.seeders === 1 ? '' : 's'}`)
  }
  return parts.length === 0 ? release.title : parts.join(' · ')
}

/**
 * `1080p · subs en · audio ja` — what the file a viewer is about to play
 * actually is (FR-P4). Height is the resolution people name; the exact pixel
 * dimensions and the duration are on the player, not in a table cell. A track
 * the source did not have is left out rather than shown as "none".
 */
export function renditionLine(rendition: EpisodeRendition): string {
  const parts: string[] = []
  if (rendition.height > 0) parts.push(`${String(rendition.height)}p`)
  if (rendition.subtitle_lang !== null && rendition.subtitle_lang !== '') {
    parts.push(`subs ${rendition.subtitle_lang}`)
  }
  if (rendition.audio_lang !== null && rendition.audio_lang !== '') {
    parts.push(`audio ${rendition.audio_lang}`)
  }
  return parts.join(' · ')
}

/**
 * The muted line under an episode's title: where the file came from, then what
 * it turned into. Both can be present — a ready episode still names its
 * release — so they join rather than replace one another, and an episode with
 * neither gets no line at all. The server only fills `rendition` once an
 * episode is `ready`, which is what keeps this from claiming a file exists.
 */
export function episodeDetailLine(episode: EpisodeOut): string | null {
  const parts: string[] = []
  if (episode.release !== null) parts.push(releaseLine(episode.release))
  if (episode.rendition !== null) {
    const rendition = renditionLine(episode.rendition)
    if (rendition !== '') parts.push(rendition)
  }
  return parts.length === 0 ? null : parts.join(' · ')
}

/**
 * Weekday + date in the browser's locale, plus the time for anything still to
 * come (a past air date only needs the day; an upcoming one is a countdown a
 * person plans around). `tz` overrides the browser's zone — the user's own
 * timezone from `/api/auth/me`, per FR-C3.
 */
export function formatAirDate(iso: string | null | undefined, tz?: string): string {
  if (iso === null || iso === undefined || iso === '') return '—'
  const at = new Date(iso)
  const time = at.getTime()
  if (Number.isNaN(time)) return '—'

  const options: Intl.DateTimeFormatOptions = {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  }
  if (time > Date.now()) {
    options.hour = 'numeric'
    options.minute = '2-digit'
  }
  if (tz !== undefined && tz !== '') options.timeZone = tz

  try {
    return new Intl.DateTimeFormat(undefined, options).format(at)
  } catch {
    // An invalid `tz` must not blank out the row.
    return new Intl.DateTimeFormat(undefined, { ...options, timeZone: undefined }).format(at)
  }
}

export function useAnimeSearch(q: string, page = 1): UseQueryResult<AnimeSearchResponse, Error> {
  const query = q.trim()

  return useQuery<AnimeSearchResponse, Error>({
    queryKey: animeSearchQueryKey(query, page),
    queryFn: () => {
      const params = new URLSearchParams({ q: query, page: String(page) })
      return apiFetch<AnimeSearchResponse>(`/api/anime/search?${params.toString()}`)
    },
    enabled: isSearchable(query),
    // Paging must not blank the grid between pages.
    placeholderData: keepPreviousData,
    // Every attempt costs the server an AniList call, and neither a rejected
    // query nor a failing upstream gets better by being asked again.
    retry: false,
  })
}

export function useAnime(id: number): UseQueryResult<AnimeDetail, Error> {
  return useQuery<AnimeDetail, Error>({
    queryKey: animeQueryKey(id),
    queryFn: () => apiFetch<AnimeDetail>(`/api/anime/${id}`),
    enabled: Number.isInteger(id) && id > 0,
    retry: false,
    // Acquisition advances on the server's clock, not on anything the viewer
    // does, so the page re-asks itself while an episode is still being fetched
    // or prepared and goes quiet the moment they have all settled (FR-A7).
    // Reading `query.state.data` rather than closing over a render's copy keeps
    // the decision on the freshest answer, including the one that ends polling.
    refetchInterval: (query) => (hasActiveEpisode(query.state.data) ? ACQUISITION_POLL_MS : false),
  })
}

export function useMyList(status?: ListStatus): UseQueryResult<MyListItem[], Error> {
  return useQuery<MyListItem[], Error>({
    queryKey: listQueryKey(status),
    queryFn: () => {
      const suffix = status === undefined ? '' : `?status=${encodeURIComponent(status)}`
      return apiFetch<MyListItem[]>(`/api/list${suffix}`)
    },
  })
}

/** The same summary carrying a new list status; identity is kept when it can be. */
function withStatus(anime: AnimeSummary, status: ListStatus | null): AnimeSummary {
  return anime.list_status === status ? anime : { ...anime, list_status: status }
}

/**
 * The schedule grid. A row's own `list_status` drives its select and
 * `following` drives its accent edge, so the two have to move together — a row
 * showing "Watching" without the edge would read as a bug for the second the
 * refetch takes.
 */
function applyToSchedule(client: QueryClient, animeId: number, status: ListStatus | null): void {
  const following = isFollowing(status)

  function patch(entry: ScheduleEntry): ScheduleEntry {
    if (entry.anime.id !== animeId) return entry
    return { ...entry, anime: withStatus(entry.anime, status), list_status: status, following }
  }

  function holds(entries: ScheduleEntry[]): boolean {
    return entries.some((entry) => entry.anime.id === animeId)
  }

  client.setQueriesData<SchedulePage>({ queryKey: [SCHEDULE_QUERY_KEY] }, (current) => {
    if (current === undefined) return current
    // A season this show does not air in is left alone, object identity and all.
    if (!current.days.some((day) => holds(day.entries)) && !holds(current.unscheduled)) {
      return current
    }
    return {
      ...current,
      days: current.days.map((day) =>
        holds(day.entries) ? { ...day, entries: day.entries.map(patch) } : day,
      ),
      unscheduled: holds(current.unscheduled)
        ? current.unscheduled.map(patch)
        : current.unscheduled,
    }
  })
}

/**
 * The home dashboard. "Behind on" is a list of *followed* shows, so a show
 * taken off the list, dropped or completed leaves it immediately rather than
 * sitting there with a status that contradicts the section it is in.
 */
function applyToHome(client: QueryClient, animeId: number, status: ListStatus | null): void {
  const followed = isFollowing(status) ? status : null

  client.setQueriesData<HomePage>({ queryKey: [HOME_QUERY_KEY] }, (current) => {
    if (current === undefined) return current
    const inBehind = current.behind.some((item) => item.anime.id === animeId)
    const inWeek = current.new_this_week.some((item) => item.anime.id === animeId)
    if (!inBehind && !inWeek) return current

    return {
      ...current,
      behind:
        followed === null
          ? current.behind.filter((item) => item.anime.id !== animeId)
          : current.behind.map((item) =>
              item.anime.id === animeId
                ? {
                    ...item,
                    anime: withStatus(item.anime, followed),
                    entry: { ...item.entry, status: followed },
                  }
                : item,
            ),
      new_this_week: inWeek
        ? current.new_this_week.map((item) =>
            item.anime.id === animeId ? { ...item, anime: withStatus(item.anime, status) } : item,
          )
        : current.new_this_week,
    }
  })
}

/**
 * The recommendations page. Its picks are a stored snapshot the server will
 * not recompute (spec §4.8 FR-R5), so there is no refetch to bridge to here:
 * the patch *is* the answer, and without it the add-to-planned select on a
 * pick would snap back the moment anything re-rendered.
 */
function applyToRecs(client: QueryClient, animeId: number, status: ListStatus | null): void {
  client.setQueriesData<RecsPage>({ queryKey: [RECS_QUERY_KEY] }, (current) => {
    if (current === undefined || current.run === null) return current
    const { run } = current
    const continuations = run.continuations ?? []

    const inPicks = run.picks.some((pick) => pick.anime.id === animeId)
    const inContinuations = continuations.some((entry) => entry.anime.id === animeId)
    if (!inPicks && !inContinuations) return current

    return {
      ...current,
      run: {
        ...run,
        picks: inPicks
          ? run.picks.map((pick) =>
              pick.anime.id === animeId ? { ...pick, anime: withStatus(pick.anime, status) } : pick,
            )
          : run.picks,
        // Only rewritten when there is something to rewrite: a run stored
        // before continuations existed must not grow an empty array here.
        ...(inContinuations
          ? {
              continuations: continuations.map((entry) =>
                entry.anime.id === animeId
                  ? { ...entry, anime: withStatus(entry.anime, status) }
                  : entry,
              ),
            }
          : {}),
      },
    }
  })
}

/** Patch every cached view of this anime so the UI never shows a stale status. */
function applyListStatus(client: QueryClient, animeId: number, entry: ListEntry | null): void {
  client.setQueryData<AnimeDetail>(animeQueryKey(animeId), (current) =>
    current === undefined
      ? current
      : { ...current, list_entry: entry, list_status: entry?.status ?? null },
  )

  client.setQueriesData<AnimeSearchResponse>(
    { queryKey: [ANIME_QUERY_KEY, 'search'] },
    (current) => {
      if (current === undefined) return current
      if (!current.results.some((anime) => anime.id === animeId)) return current
      return {
        ...current,
        results: current.results.map((anime) =>
          anime.id === animeId ? { ...anime, list_status: entry?.status ?? null } : anime,
        ),
      }
    },
  )

  // Following a show changes its highlight on the schedule and can add or
  // remove a "behind on" card (roadmap M4). Both are server-computed, so the
  // patch below is only good enough to bridge the refetch that follows it.
  applyToSchedule(client, animeId, entry?.status ?? null)
  applyToHome(client, animeId, entry?.status ?? null)
  applyToRecs(client, animeId, entry?.status ?? null)

  void client.invalidateQueries({ queryKey: [LIST_QUERY_KEY] })
  void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY, 'search'] })
  void client.invalidateQueries({ queryKey: [SCHEDULE_QUERY_KEY] })
  void client.invalidateQueries({ queryKey: [HOME_QUERY_KEY] })
}

export function useSetListEntry(): UseMutationResult<ListEntry, Error, SetListEntryInput> {
  const client = useQueryClient()

  return useMutation<ListEntry, Error, SetListEntryInput>({
    mutationFn: ({ animeId, ...body }) =>
      apiFetch<ListEntry>(`/api/list/${animeId}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),
    onSuccess: (entry, { animeId }) => {
      applyListStatus(client, animeId, entry)
    },
  })
}

export interface RetryTranscodeInput {
  /** The show whose page is on screen; what the refetch is keyed on. */
  animeId: number
  episodeId: number
  /** Re-encode an episode that is already `ready`, rather than retry a failure. */
  force?: boolean
}

/**
 * Put a failed episode back through the transcoder (spec §4.4 FR-P4, admin
 * only). The server answers 202 and does the work in a job, so there is
 * nothing in the response worth reading: the episode goes back to `preparing`
 * and the show page's own poll (FR-A7) carries it from there. Invalidating the
 * detail query is what starts that poll again, since a page of settled
 * episodes has stopped asking.
 */
export function useRetryTranscode(): UseMutationResult<null, Error, RetryTranscodeInput> {
  const client = useQueryClient()

  return useMutation<null, Error, RetryTranscodeInput>({
    mutationFn: ({ episodeId, force }) => {
      const suffix = force === true ? '?force=true' : ''
      return apiFetch<null>(`/api/episodes/${episodeId}/transcode${suffix}`, { method: 'POST' })
    },
    onSuccess: (_result, { animeId }) => {
      void client.invalidateQueries({ queryKey: animeQueryKey(animeId) })
    },
  })
}

export function useRemoveListEntry(): UseMutationResult<null, Error, number> {
  const client = useQueryClient()

  return useMutation<null, Error, number>({
    mutationFn: (animeId) => apiFetch<null>(`/api/list/${animeId}`, { method: 'DELETE' }),
    onSuccess: (_result, animeId) => {
      applyListStatus(client, animeId, null)
    },
  })
}
