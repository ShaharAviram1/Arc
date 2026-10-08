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
import {
  noteServerPosition,
  positionWrittenAt,
  recallPosition,
  recallServerPosition,
  type LocalPosition,
  type ServerPosition,
} from '@/offline/cache'
import {
  downloadedNeighbours,
  downloads,
  type DownloadManager,
  type DownloadRecord,
  type Downloads,
} from '@/offline/downloads'
import { crossesCompletion, outbox, type Outbox } from '@/offline/outbox'
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
  /**
   * Always under `/media/…`, behind the session cookie (spec §5.4). **Null**
   * for an `offline_only` episode (M19): there is nothing to stream, and it
   * must never reach a `<video>` as a source.
   */
  playlist_url: string | null
  /** Seconds, from the rendition — the fallback when the media has none yet. */
  duration: number
  /**
   * A trip episode that lives only on this account's devices (FR-A12, M19):
   * not ready, no rendition, `playlist_url` null. The player plays the
   * device's copy, or says to keep it offline first. Optional on the wire.
   */
  offline_only?: boolean
  /** Seconds to resume from, or null when there is nothing to resume. */
  resume_position: number | null
  /**
   * When the server's position was written (ISO; the `watch_progress` row's
   * `updated_at`), or null with no row. Compared with the device's position
   * (`newerResume`, 2026-10-08). Absent from a server older than that, where
   * the device's `srv:` record stands in.
   */
  resume_at?: string | null
  previous: EpisodeRef | null
  next: EpisodeRef | null
  /**
   * Client-side only: this payload was rebuilt on the device from a download
   * (FR-S9) because the server could not be reached, or no longer has the
   * rendition. `playlist_url` is then null and the episode plays from its file.
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

/** A recorded length when there is one, else the length this device last played. */
function knownDuration(recorded: number, local: LocalPosition | null): number {
  if (Number.isFinite(recorded) && recorded > 0) return recorded
  return local !== null && Number.isFinite(local.duration_s) && local.duration_s > 0
    ? local.duration_s
    : 0
}

/** The server's FR-S2 filter (`arc.services.playback.progress.resume_position`). */
function serverResumeOf(position: number | null, duration: number): number | null {
  if (position === null || !Number.isFinite(position) || position <= RESUME_MIN_SECONDS) return null
  if (duration > 0 && position >= RESUME_MAX_FRACTION * duration) return null
  return position
}

/** Two positions closer than this are the same write read back. */
const SAME_POSITION_S = 0.5

/**
 * Whose position the player opens at, online: the server's or this device's
 * (FR-S2, FR-S9; owner 2026-10-08, "is there no continue watching from the
 * same spot for local videos?"). Pure.
 *
 * The newer one wins:
 *
 * 1. No local position → the server's.
 * 2. Something for this episode is still waiting in the outbox (watched with
 *    no network, not synced yet) → the device's: the server has not heard it.
 * 3. The server says when its position was written (`resume_at`, the
 *    `watch_progress` row's `updated_at`): the device's wins iff it was
 *    written later, or the server has no row (`null`).
 *
 * **Fallback, for a server older than `resume_at`** (field absent): "newer" is
 * worked out from what this device knows instead.
 *
 * 4. The server's position is still the one this device last saw it hold
 *    (`seen`: the answer it last opened with, or its last accepted report or
 *    synced item), and the device wrote a position after it saw that → the
 *    device's: nobody else has moved the server since, and the device has.
 * 5. Otherwise — the server's moved (another device watched it), the device
 *    has not watched since it last looked, or the device has never looked
 *    (`seen` null) → the server's.
 *
 * Offline there is no server answer; the device's position is used outright
 * ({@link offlinePlayInfo}).
 */
export function newerResume(
  serverResume: number | null,
  serverDuration: number,
  local: LocalPosition | null,
  seen: ServerPosition | null,
  queued: boolean,
  serverAt?: string | null,
): 'device' | 'server' {
  if (local === null) return 'server'
  if (queued) return 'device'
  if (serverAt === null) return 'device'
  if (serverAt !== undefined) {
    const at = Date.parse(serverAt)
    if (Number.isFinite(at)) return positionWrittenAt(local) > at ? 'device' : 'server'
  }
  if (seen === null) return 'server'
  const expected = serverResumeOf(
    seen.position_s,
    serverDuration > 0 ? serverDuration : seen.duration_s,
  )
  const unchanged =
    expected === null
      ? serverResume === null
      : serverResume !== null && Math.abs(serverResume - expected) <= SAME_POSITION_S
  if (!unchanged) return 'server'
  const seenAt = Date.parse(seen.at)
  return positionWrittenAt(local) > (Number.isFinite(seenAt) ? seenAt : 0) ? 'device' : 'server'
}

/**
 * The player's payload rebuilt from a download, for when the server cannot
 * answer (FR-S9). Pure: everything it needs is passed in.
 *
 * - **Resume** is the last position watched *on this device*: the server's
 *   is unreachable, and the device's is the newest the viewer can have made
 *   anyway, since nothing else could have reached this iPad meanwhile.
 * - **Duration** is the snapshot's, or — for a trip-only episode, whose
 *   snapshot has none (the server had no rendition to measure) — the length
 *   the device's player last saw, so the resume has a ceiling to check.
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
    playlist_url: null,
    duration: knownDuration(record.snapshot.duration, local),
    resume_position: local === null ? null : local.position_s,
    previous: refOf(previous),
    next: refOf(next),
    from_device: true,
  }
}

/**
 * Previous / next, preferring the device (FR-S9, M19): where the server's
 * neighbour cannot be played — not ready (a trip-only episode is never
 * ready), or none — the nearest downloaded episode of the show that way is
 * offered instead. A ready server neighbour is kept: it is the adjacent one.
 */
export function withDeviceNeighbours(info: PlayInfo, records: Downloads): PlayInfo {
  if (info.from_device === true) return info
  const anime = info.anime.id
  const number = info.episode.number
  const kept = Object.values(records)
    .filter((record) => record.animeId === anime && record.state === 'downloaded')
    .sort((a, b) => a.snapshot.episode.number - b.snapshot.episode.number)
  if (kept.length === 0) return info
  const onDevice = (episodeId: number | undefined) =>
    kept.find((record) => record.episodeId === episodeId) ?? null
  let previous = info.previous
  let next = info.next
  if (previous === null || !previous.ready) {
    const exact = onDevice(previous?.id)
    const nearest = [...kept].reverse().find((record) => record.snapshot.episode.number < number)
    previous = refOf(exact ?? nearest ?? null) ?? previous
  }
  if (next === null || !next.ready) {
    const exact = onDevice(next?.id)
    const nearest = kept.find((record) => record.snapshot.episode.number > number)
    next = refOf(exact ?? nearest ?? null) ?? next
  }
  return previous === info.previous && next === info.next ? info : { ...info, previous, next }
}

export interface PlayInfoDeps {
  fetch: (episodeId: number) => Promise<PlayInfo>
  manager: DownloadManager
  recall: (userId: number, episodeId: number) => Promise<LocalPosition | null>
  /** What this device last knew the server to hold ({@link newerResume}). */
  recallSeen: (userId: number, episodeId: number) => Promise<ServerPosition | null>
  noteSeen: (
    userId: number,
    episodeId: number,
    position: number | null,
    duration: number,
  ) => Promise<void>
  /** Whether anything for this episode is still waiting in the outbox. */
  queued: (userId: number, episodeId: number) => boolean
  pending: () => ReadonlyMap<number, boolean>
}

function defaultPlayInfoDeps(): PlayInfoDeps {
  return {
    fetch: (episodeId) => apiFetch<PlayInfo>(`/api/episodes/${String(episodeId)}/play`),
    manager: downloads(),
    recall: recallPosition,
    recallSeen: recallServerPosition,
    noteSeen: noteServerPosition,
    queued: (userId, episodeId) =>
      outbox()
        .getSnapshot()
        .pending.some((record) => record.user_id === userId && record.episode_id === episodeId),
    pending: () => pendingWatched(outbox().getSnapshot().pending),
  }
}

/**
 * `GET /api/episodes/{id}/play`, or — when the server cannot be reached, or no
 * longer has a rendition (retention removed it) — the payload rebuilt from
 * this account's download of the episode. Anything else, and any episode not
 * downloaded, fails exactly as before.
 *
 * Online, the resume position is the newer of the server's and this device's
 * ({@link newerResume}), for every episode — streamed or played from the
 * device — and the duration falls back to the one this device last played
 * when the server has none (a trip-only episode, M19).
 */
export async function loadPlayInfo(
  episodeId: number,
  deps: PlayInfoDeps = defaultPlayInfoDeps(),
): Promise<PlayInfo> {
  let answer: PlayInfo
  try {
    answer = await deps.fetch(episodeId)
  } catch (error) {
    if (!isUnreachable(error) && !isStatus404(error)) throw error
    await deps.manager.whenHydrated()
    const owner = deps.manager.ownerId
    const record = deps.manager.record(episodeId)
    if (owner === null || record === undefined || record.state !== 'downloaded') throw error
    const local = await deps.recall(owner, episodeId)
    return offlinePlayInfo(record, deps.manager.getSnapshot(), local, deps.pending())
  }
  await deps.manager.whenHydrated()
  answer = await withDevicePosition(answer, episodeId, deps)
  const neighbourMissing =
    answer.previous === null || !answer.previous.ready || answer.next === null || !answer.next.ready
  if (!neighbourMissing) return answer
  return withDeviceNeighbours(answer, deps.manager.getSnapshot())
}

/** The server's answer with {@link newerResume}'s position in it. Never throws. */
async function withDevicePosition(
  answer: PlayInfo,
  episodeId: number,
  deps: PlayInfoDeps,
): Promise<PlayInfo> {
  const owner = deps.manager.ownerId
  if (owner === null) return answer
  try {
    const [local, seen] = await Promise.all([
      deps.recall(owner, episodeId),
      deps.recallSeen(owner, episodeId),
    ])
    const duration = knownDuration(answer.duration, local)
    const winner = newerResume(
      answer.resume_position,
      duration,
      local,
      seen,
      deps.queued(owner, episodeId),
      answer.resume_at,
    )
    if (winner === 'device' && local !== null) {
      // `seen` is left as it was: the server still holds what it said then,
      // and the device's position is still the newer one until it is sent.
      return { ...answer, duration, resume_position: local.position_s }
    }
    await deps.noteSeen(owner, episodeId, answer.resume_position, duration)
    return duration === answer.duration ? answer : { ...answer, duration }
  } catch {
    // A device store that cannot be read is no reason not to play.
    return answer
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
  // The server now holds this position: what the next open compares with (2026-10-08).
  if (owner !== null) {
    noteServerPosition(owner, body.episode_id, body.position_s, body.duration_s).catch(
      () => undefined,
    )
  }
  if (owner !== null && result.completed) box.noteCompleted(owner, body.episode_id, true)
  // The server accepted a completion at this viewing: a report at or past the
  // mark that it answered `completed`. A report early in a rewatch of an
  // episode completed long ago answers `completed` too, and is not one.
  if (owner !== null && result.completed && crossesCompletion(body.position_s, body.duration_s)) {
    box.announceWatched(owner, body.episode_id, true)
  }
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
  if (owner !== null) {
    box.noteCompleted(owner, episodeId, watched)
    box.announceWatched(owner, episodeId, watched)
  }
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
