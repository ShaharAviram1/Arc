/**
 * Playback data layer (spec §4.5 FR-S1–FR-S6, §4.6 FR-W3, roadmap M8).
 *
 * `GET /api/episodes/{id}/play` is the only thing the player page asks for:
 * it carries the playlist URL, the transcode's own duration, where the viewer
 * left off, and the episodes either side. The server decides all of it, so the
 * client never derives a media path from a route parameter (spec §7).
 *
 * Progress writes come from two places — the reporter's timer and the manual
 * "mark watched" button — and both can change how far through a show the
 * viewer is. Anything that lands a completion therefore invalidates the show
 * page and the home dashboard, which are the two views that show it.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'
import { animeQueryKey, ANIME_QUERY_KEY, type AnimeSummary, type EpisodeOut } from '@/lib/anime'
import { HOME_QUERY_KEY } from '@/lib/schedule'

/** Where `POST` progress reports go; the unload beacon uses it too. */
export const PROGRESS_PATH = '/api/progress'

export const PLAY_QUERY_KEY = 'play'

export function playQueryKey(episodeId: number): readonly [string, number] {
  return [PLAY_QUERY_KEY, episodeId]
}

/**
 * The episode before or after the one being played. `ready` is the only thing
 * the player acts on — a neighbour still downloading is offered as a
 * disabled control rather than hidden, so the viewer can see it exists.
 */
export interface EpisodeRef {
  id: number
  number: number
  state: string
  ready: boolean
}

/** `GET /api/episodes/{id}/play`; 404 until the episode is `ready`. */
export interface PlayInfo {
  episode: EpisodeOut
  anime: AnimeSummary
  /** Always under `/media/…`, behind the session cookie (spec §5.4). */
  playlist_url: string
  /** Seconds, from the rendition — the fallback when the media has none yet. */
  duration: number
  /** Seconds to resume from, or null when there is nothing to resume. */
  resume_position: number | null
  previous: EpisodeRef | null
  next: EpisodeRef | null
}

/** What every progress write answers with (FR-S4). */
export interface ProgressResult {
  completed: boolean
  /** True only on the write that crossed the threshold; drives the overlay. */
  newly_completed: boolean
  /** The viewer's list progress after the write, when it advanced. */
  list_progress: number | null
}

export interface ReportProgressInput {
  episode_id: number
  position_s: number
  duration_s: number
}

export interface WatchedInput {
  episodeId: number
  /** The show whose page to refresh; not sent to the server. */
  animeId: number
}

/** Below this a resume is not worth doing; above it the episode is over (FR-S2). */
export const RESUME_MIN_SECONDS = 10
export const RESUME_MAX_FRACTION = 0.95

/**
 * Whether a stored position is worth seeking to (FR-S2). The server applies
 * the same rule, but the client holds the media's own duration, which can
 * differ from the recorded one by a second or two.
 */
export function shouldResume(position: number | null, duration: number): boolean {
  if (position === null || !Number.isFinite(position) || position <= RESUME_MIN_SECONDS) {
    return false
  }
  if (!Number.isFinite(duration) || duration <= 0) return true
  return position < duration * RESUME_MAX_FRACTION
}

/** `753` → `12:33`; `3753` → `1:02:33`. Anything unusable reads as `0:00`. */
export function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00'
  const whole = Math.floor(seconds)
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const secs = whole % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return hours > 0
    ? `${String(hours)}:${pad(minutes)}:${pad(secs)}`
    : `${String(minutes)}:${pad(secs)}`
}

/** The toast shown after an automatic seek, so a jump is never unexplained. */
export function resumeLabel(seconds: number): string {
  return `Resumed from ${formatClock(seconds)}`
}

export function usePlayInfo(episodeId: number): UseQueryResult<PlayInfo, Error> {
  return useQuery<PlayInfo, Error>({
    queryKey: playQueryKey(episodeId),
    queryFn: () => apiFetch<PlayInfo>(`/api/episodes/${episodeId}/play`),
    enabled: Number.isInteger(episodeId) && episodeId > 0,
    // A 404 here means "not ready", which asking again will not change; the
    // resume position must also not be re-read behind a playing video, hence
    // the infinite stale time for as long as the page is mounted.
    retry: false,
    staleTime: Infinity,
    refetchOnMount: false,
    // But it must be re-read on the way back in. Watching an episode moves its
    // resume position, so a cached answer from before is wrong the moment the
    // viewer returns — going next then previous would resume from where they
    // were an episode ago. Dropping the entry on unmount makes re-entry a
    // fresh request without weakening the rule above.
    gcTime: 0,
  })
}

/**
 * Everything a completion changes, once one has happened. `['anime']` is
 * invalidated by prefix because the row that gains its ✓ lives on the show
 * page, and the summary that gains its progress lives in search results.
 */
function invalidateAfterCompletion(client: QueryClient, animeId?: number): void {
  const animeKey = animeId === undefined ? [ANIME_QUERY_KEY] : animeQueryKey(animeId)
  void client.invalidateQueries({ queryKey: animeKey })
  void client.invalidateQueries({ queryKey: [HOME_QUERY_KEY] })
}

/**
 * One progress write (FR-S3). Never retried: a report is a position at a
 * moment, and the next tick carries a better one, so a retry would write a
 * stale number over a fresh one.
 */
export function useReportProgress(): UseMutationResult<ProgressResult, Error, ReportProgressInput> {
  const client = useQueryClient()

  return useMutation<ProgressResult, Error, ReportProgressInput>({
    mutationFn: (body) =>
      apiFetch<ProgressResult>(PROGRESS_PATH, { method: 'POST', body: JSON.stringify(body) }),
    retry: false,
    onSuccess: (result) => {
      if (!result.newly_completed) return
      invalidateAfterCompletion(client)
    },
  })
}

/** Mark an episode watched by hand (FR-W3): the same effect as finishing it. */
export function useMarkWatched(): UseMutationResult<ProgressResult, Error, WatchedInput> {
  const client = useQueryClient()

  return useMutation<ProgressResult, Error, WatchedInput>({
    mutationFn: ({ episodeId }) =>
      apiFetch<ProgressResult>(`/api/episodes/${episodeId}/watched`, { method: 'POST' }),
    onSuccess: (_result, { animeId }) => {
      invalidateAfterCompletion(client, animeId)
    },
  })
}

/** Undo a mark. The list progress the server rolls back is its own business. */
export function useUnmarkWatched(): UseMutationResult<ProgressResult, Error, WatchedInput> {
  const client = useQueryClient()

  return useMutation<ProgressResult, Error, WatchedInput>({
    mutationFn: ({ episodeId }) =>
      apiFetch<ProgressResult>(`/api/episodes/${episodeId}/watched`, { method: 'DELETE' }),
    onSuccess: (_result, { animeId }) => {
      invalidateAfterCompletion(client, animeId)
    },
  })
}
