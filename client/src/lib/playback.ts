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
import { ApiError, apiFetch, isOffline, isUnreachable } from '@/lib/api'
import { recallPosition, type LocalPosition } from '@/offline/cache'
import {
  downloadedNeighbours,
  downloads,
  type DownloadManager,
  type DownloadRecord,
  type Downloads,
} from '@/offline/downloads'
import { outbox, type Outbox } from '@/offline/outbox'
import { pendingWatched } from '@/offline/useOutbox'
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
  /**
   * Client-side only: this payload was rebuilt on the device from a download
   * (FR-S9) because the server could not be reached, or no longer has the
   * rendition. `playlist_url` is then empty and the episode plays from its file.
   */
  from_device?: boolean
}

/** What every progress write answers with (FR-S4). */
export interface ProgressResult {
  completed: boolean
  /** True only on the write that crossed the threshold; drives the overlay. */
  newly_completed: boolean
  /**
   * The viewer's list progress after the write, when the write moved it —
   * up for a completion (FR-S4) and down by one for an un-mark of the latest
   * watched episode (FR-S4 as revised 2026-09-13). Null when nothing moved.
   */
  list_progress: number | null
  /**
   * Client-side only: the write could not reach the server and is kept in the
   * on-device outbox for replay (FR-S8). The other fields then describe what
   * the viewer did, not what the server said.
   */
  queued?: boolean
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

/**
 * Why an episode *under* the latest watched one offers nothing to press
 * (FR-W5). The un-mark moves the list by one episode from the top, so taking
 * back episode 4 of a list that says 9 is not something the viewer can ask
 * for. Shared by the show page's row control and the player's toggle, so the
 * one explanation Arc has for "this tick has no undo" is written once.
 */
export const WATCHED_BY_PROGRESS_HINT = 'Unwatch from the latest watched episode down'

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
  // Not knowing how long the episode is (a fragmented MP4 can report
  // `Infinity` or `NaN`, and the caller had no recorded length to fall back
  // on) is a reason *not* to seek: a jump to a stored position past the end
  // is worse than starting at zero (FR-S9).
  if (!Number.isFinite(duration) || duration <= 0) return false
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

function refOf(record: DownloadRecord | null): EpisodeRef | null {
  if (record === null) return null
  return {
    id: record.episodeId,
    number: record.snapshot.episode.number,
    state: 'ready',
    ready: true,
  }
}

/**
 * The player's payload rebuilt from a download, for when the server cannot
 * answer (FR-S9). Pure: everything it needs is passed in.
 *
 * - **Resume** is the last position watched *on this device*: the server's
 *   is unreachable, and the device's is the newest the viewer can have made
 *   anyway, since nothing else could have reached this iPad meanwhile.
 * - **Previous / next** are the nearest *downloaded* episodes of the show,
 *   the only ones that can play — never a link to one that cannot.
 * - **Watched** is what the download saw, overridden by a mark or un-mark
 *   still waiting in the outbox.
 */
export function offlinePlayInfo(
  record: DownloadRecord,
  records: Downloads,
  local: LocalPosition | null,
  pending: ReadonlyMap<number, boolean> = new Map(),
): PlayInfo {
  const { previous, next } = downloadedNeighbours(records, record)
  const queued = pending.get(record.episodeId)
  const episode: EpisodeOut =
    queued === undefined
      ? record.snapshot.episode
      : {
          ...record.snapshot.episode,
          watched: queued,
          watched_source: queued ? ('arc' as const) : null,
        }
  return {
    episode,
    anime: record.snapshot.anime,
    playlist_url: '',
    duration: record.snapshot.duration,
    resume_position: local === null ? null : local.position_s,
    previous: refOf(previous),
    next: refOf(next),
    from_device: true,
  }
}

export interface PlayInfoDeps {
  fetch: (episodeId: number) => Promise<PlayInfo>
  manager: DownloadManager
  recall: (userId: number, episodeId: number) => Promise<LocalPosition | null>
  pending: () => ReadonlyMap<number, boolean>
}

function defaultPlayInfoDeps(): PlayInfoDeps {
  return {
    fetch: (episodeId) => apiFetch<PlayInfo>(`/api/episodes/${String(episodeId)}/play`),
    manager: downloads(),
    recall: recallPosition,
    pending: () => pendingWatched(outbox().getSnapshot().pending),
  }
}

/**
 * `GET /api/episodes/{id}/play`, or — when the server cannot be reached, or no
 * longer has a rendition (retention removed it) — the payload rebuilt from
 * this account's download of the episode. Anything else, and any episode not
 * downloaded, fails exactly as before.
 */
export async function loadPlayInfo(
  episodeId: number,
  deps: PlayInfoDeps = defaultPlayInfoDeps(),
): Promise<PlayInfo> {
  try {
    return await deps.fetch(episodeId)
  } catch (error) {
    if (!isUnreachable(error) && !isStatus404(error)) throw error
    await deps.manager.whenHydrated()
    const owner = deps.manager.ownerId
    const record = deps.manager.record(episodeId)
    if (owner === null || record === undefined || record.state !== 'downloaded') throw error
    const local = await deps.recall(owner, episodeId)
    return offlinePlayInfo(record, deps.manager.getSnapshot(), local, deps.pending())
  }
}

function isStatus404(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

export function usePlayInfo(episodeId: number): UseQueryResult<PlayInfo, Error> {
  return useQuery<PlayInfo, Error>({
    queryKey: playQueryKey(episodeId),
    queryFn: () => loadPlayInfo(episodeId),
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
 * Whether a failed write is worth keeping for later (FR-S8) rather than
 * reporting. No response at all, a server error, an expired session and a
 * rate limit are all "not now"; a 404 or a 422 is the server's final word,
 * and replaying it later would only be rejected again.
 */
export function shouldQueue(error: unknown): boolean {
  if (isOffline(error)) return true
  if (!(error instanceof ApiError)) return false
  return error.status >= 500 || error.status === 401 || error.status === 408 || error.status === 429
}

/**
 * One progress report (FR-S3), falling into the outbox when it cannot be
 * delivered. Online it is exactly the `POST /api/progress` it always was.
 * With nobody to attribute the record to (no account has signed in on this
 * page) the error stands, because a record nobody owns could never be sent.
 */
export async function sendProgress(
  body: ReportProgressInput,
  box: Outbox = outbox(),
): Promise<ProgressResult> {
  let result: ProgressResult
  try {
    result = await apiFetch<ProgressResult>(PROGRESS_PATH, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  } catch (error) {
    const owner = box.recordingAs
    if (owner === null || !shouldQueue(error)) throw error
    await box.recordPosition(owner, body.episode_id, body.position_s, body.duration_s)
    return { completed: false, newly_completed: false, list_progress: null, queued: true }
  }
  // Remembered so the exit beacon past the mark queues nothing: the server
  // has already said this episode is done (B1, 2026-10-05).
  const owner = box.recordingAs
  if (owner !== null && result.completed) box.noteCompleted(owner, body.episode_id, true)
  return result
}

/** FR-W3's mark, or FR-S4's un-mark, falling into the outbox when offline (FR-S8). */
export async function sendWatched(
  episodeId: number,
  watched: boolean,
  box: Outbox = outbox(),
): Promise<ProgressResult> {
  let result: ProgressResult
  try {
    result = await apiFetch<ProgressResult>(`/api/episodes/${String(episodeId)}/watched`, {
      method: watched ? 'POST' : 'DELETE',
    })
  } catch (error) {
    const owner = box.recordingAs
    if (owner === null || !shouldQueue(error)) throw error
    if (watched) await box.recordCompletion(owner, episodeId)
    else await box.recordUnmark(owner, episodeId)
    return { completed: watched, newly_completed: false, list_progress: null, queued: true }
  }
  const owner = box.recordingAs
  if (owner !== null) box.noteCompleted(owner, episodeId, watched)
  return result
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
    mutationFn: (body) => sendProgress(body),
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
    mutationFn: ({ episodeId }) => sendWatched(episodeId, true),
    onSuccess: (result, { animeId }) => {
      // Queued offline: there is nothing new on the server to refetch yet.
      if (result.queued === true) return
      invalidateAfterCompletion(client, animeId)
    },
  })
}

/**
 * Undo a mark (FR-W3, FR-W5). It clears Arc's own completion row and, when the
 * episode is the latest the viewer has watched, lowers list progress by one —
 * the single place Arc lowers MyAnimeList's progress, and only because the
 * viewer pressed it (FR-S4, revised 2026-09-13). Offered only where
 * `watched_source` is `arc`, which is exactly where it would change something.
 */
export function useUnmarkWatched(): UseMutationResult<ProgressResult, Error, WatchedInput> {
  const client = useQueryClient()

  return useMutation<ProgressResult, Error, WatchedInput>({
    mutationFn: ({ episodeId }) => sendWatched(episodeId, false),
    onSuccess: (result, { animeId }) => {
      if (result.queued === true) return
      invalidateAfterCompletion(client, animeId)
    },
  })
}
