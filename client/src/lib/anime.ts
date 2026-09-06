/**
 * Catalogue and list-state data layer (spec §4.1 FR-C1/FR-C2, §4.6 FR-W2,
 * roadmap M3).
 *
 * Search results, the show page and "my list" all describe the same anime, and
 * all three carry the viewer's list status. Setting a status therefore has to
 * land in every cache that shows it: the mutation patches the show and search
 * caches directly (so a select never snaps back to its old value) and
 * invalidates the list queries, which are cheap to refetch.
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

/** Arc's own per-episode state machine (spec §6). */
export interface EpisodeOut {
  id: number
  number: number
  title: string | null
  air_at: string | null
  /**
   * True when `air_at` was synthesised from a MAL broadcast slot because
   * AniList was unavailable (FR-C6); AniList data replaces it later.
   */
  air_at_estimated: boolean
  aired: boolean
  state: string
  watched: boolean
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
export interface AnimeDetail extends Omit<AnimeSummary, 'episodes'> {
  episodes: EpisodeOut[]
  episode_count: number | null
  synopsis: string | null
  genres: string[]
  studio: string | null
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
const BUSY = 'text-[var(--arc-accent)] border-[var(--arc-accent)]/40 bg-[var(--arc-accent)]/10'
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

  void client.invalidateQueries({ queryKey: [LIST_QUERY_KEY] })
  void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY, 'search'] })
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

export function useRemoveListEntry(): UseMutationResult<null, Error, number> {
  const client = useQueryClient()

  return useMutation<null, Error, number>({
    mutationFn: (animeId) => apiFetch<null>(`/api/list/${animeId}`, { method: 'DELETE' }),
    onSuccess: (_result, animeId) => {
      applyListStatus(client, animeId, null)
    },
  })
}
