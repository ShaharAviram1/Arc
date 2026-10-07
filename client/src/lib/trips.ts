/**
 * Trips on the device (spec FR-A12, FR-S9, architecture §5.4d / §5.4e, M19 T6).
 *
 * "Prepare for a trip" asks the server for small offline copies of the next X
 * aired episodes of a show; the device keeps each as it becomes available
 * (`offline/useTripAutoKeep.ts`) and tells the server when it has one. This
 * module is the wire shapes, the queries and mutations, and the few pure
 * rules the show page and the Downloads page share.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { ApiError, apiFetch } from '@/lib/api'
import { animeQueryKey, type AnimeDetail, type EpisodeOut } from '@/lib/anime'
import { errorDetail } from '@/lib/auth'
import { codecsPlayable, type DownloadRecord, type Downloads } from '@/offline/downloads'
import { deviceName, estimate } from '@/offline/opfs'

/** Where one trip episode stands on the server (`TripEpisodeOut.phase`). */
export type TripPhase =
  | 'searching'
  | 'downloading'
  | 'preparing'
  | 'available'
  | 'delivered'
  | 'expired'
  | 'unavailable'
  | 'waiting_space'

export type TripState = 'active' | 'finished' | 'cancelled' | 'expired'

export interface TripEpisode {
  episode_id: number
  number: number
  phase: TripPhase
  /** 0–1 while the server downloads or makes the copy; 1 once available; else null. */
  progress: number | null
  /** The copy's bytes, once available. */
  size: number | null
  /** Whether a device confirmed it holds the copy. */
  delivered: boolean
  /**
   * Where the copy downloads from (`/media/{id}/offline.mp4`), built by the
   * server, while the copy exists (`available` / `delivered`); else null —
   * not downloadable yet. Optional on the wire for an older payload.
   */
  url?: string | null
}

/** `TripOut`: `POST /api/anime/{id}/trip`, `GET /api/trips/current`, the show's `trip`. */
export interface Trip {
  id: number
  anime_id: number
  anime_title: string
  first_number: number
  last_number: number
  /** How many episodes the trip took: fewer than asked when fewer have aired. */
  count: number
  state: TripState
  created_at: string
  deadline_at: string
  episodes: TripEpisode[]
}

export const TRIP_QUERY_KEY = ['trip', 'current'] as const

/** How often the auto-keep hook asks about the current trip while Arc is open. */
export const TRIP_POLL_MS = 60_000

/**
 * The most episodes one trip may take when the show payload does not say
 * (`AnimeDetail.trip_limits.max_episodes`, the admin's `trip_max_episodes`,
 * is what the stepper uses; this is the ceiling, for an older payload). The
 * server's 422 `count_out_of_range` stays the backstop, said in a sentence.
 */
export const TRIP_MAX_EPISODES = 50

/** What one small copy roughly weighs (720p H.264, CRF 26): the estimate, not a promise. */
export const TRIP_EPISODE_ESTIMATE_BYTES = 100_000_000

/**
 * "500 MB", "1.2 GB": the stepper's size estimate, in round decimal units
 * because it is a guess ({@link TRIP_EPISODE_ESTIMATE_BYTES} per episode).
 */
export function tripEstimateLabel(count: number): string {
  const bytes = count * TRIP_EPISODE_ESTIMATE_BYTES
  if (bytes < 1_000_000_000) return `${String(Math.round(bytes / 1_000_000))} MB`
  return `${(bytes / 1_000_000_000).toFixed(1).replace(/\.0$/, '')} GB`
}

/**
 * What the room check reckons one episode needs: the estimate with a margin,
 * since the browser's figures are themselves an estimate (owner, 2026-10-07).
 */
export const TRIP_FIT_BYTES_PER_EPISODE = 110_000_000

/** What the browser reports about Arc's storage (`navigator.storage.estimate()`). */
export interface StorageRoom {
  usage: number
  quota: number
}

/**
 * How many trip episodes the browser says there is room for, or `null` when
 * it says nothing useful. Only ever a cap on the stepper with a sentence:
 * on iPadOS the figure is the browser's allowance, and a full iPad can still
 * report gigabytes free, so this never stands in for the write itself.
 */
export function tripRoomFor(room: StorageRoom | null): number | null {
  if (room === null || !(room.quota > 0)) return null
  const free = Math.max(0, room.quota - room.usage)
  return Math.floor(free / TRIP_FIT_BYTES_PER_EPISODE)
}

/** `navigator.storage.estimate()` for the trip card, read when it opens. */
export function useStorageRoom(enabled: boolean): UseQueryResult<StorageRoom | null, Error> {
  return useQuery<StorageRoom | null, Error>({
    queryKey: ['storage', 'estimate'],
    queryFn: () => estimate(),
    enabled,
    staleTime: 0,
    retry: false,
  })
}

/** The codecs the server makes copies in by default, for the device check. */
export const DEFAULT_COPY_CODECS = 'avc1.640028'

/**
 * The URL a trip episode's copy downloads from, when it can be downloaded:
 * the server's own `url`, for an `available` episode. Never assembled here.
 */
export function tripCopyUrl(episode: TripEpisode): string | null {
  return episode.phase === 'available' ? (episode.url ?? null) : null
}

/** The phases in which the server is still working on an episode. */
const WORKING: ReadonlySet<TripPhase> = new Set<TripPhase>([
  'searching',
  'downloading',
  'preparing',
  'waiting_space',
])

export function isPhaseWorking(phase: TripPhase): boolean {
  return WORKING.has(phase)
}

/** Whether the show page should keep asking: anything on the trip is still moving. */
export function tripInFlight(trip: Trip | null | undefined): boolean {
  return trip?.episodes.some((episode) => isPhaseWorking(episode.phase)) === true
}

/** The phase in words, for the panel's rows and the episode row's state. */
export const PHASE_LABEL: Record<TripPhase, string> = {
  searching: 'Looking for it',
  downloading: 'Downloading to the server',
  preparing: 'Making the small copy',
  available: 'Ready for this device',
  delivered: 'On your device',
  expired: 'Expired',
  unavailable: 'Not available',
  waiting_space: 'Waiting for disk space',
}

/**
 * Where the viewer is in the show, as the server counts it (FR-W5's
 * `watched_through`): the list's progress or the furthest episode marked
 * watched, whichever is further on.
 */
export function watchedThroughOf(anime: Pick<AnimeDetail, 'episodes' | 'list_entry'>): number {
  const progress = anime.list_entry?.progress ?? 0
  return anime.episodes.reduce(
    (furthest, episode) =>
      episode.watched && episode.number > furthest ? episode.number : furthest,
    progress,
  )
}

/** The aired episodes after the viewer's progress, in order: what a trip can take. */
export function tripCandidates(anime: Pick<AnimeDetail, 'episodes' | 'list_entry'>): EpisodeOut[] {
  const through = watchedThroughOf(anime)
  return anime.episodes
    .filter((episode) => episode.aired && episode.number > through)
    .sort((a, b) => a.number - b.number)
}

/** The stepper's ceiling: the server's cap, or how many have aired after the viewer's progress. */
export function tripCountMax(
  anime: Pick<AnimeDetail, 'episodes' | 'list_entry' | 'trip_limits'>,
): number {
  const cap = anime.trip_limits?.max_episodes ?? TRIP_MAX_EPISODES
  return Math.max(0, Math.min(cap, tripCandidates(anime).length))
}

/** "Episodes 4–15" (or "Episode 4") for the first `count` candidates. */
export function tripRangeLabel(candidates: readonly EpisodeOut[], count: number): string {
  const first = candidates[0]
  const last = candidates[Math.min(count, candidates.length) - 1]
  if (first === undefined || last === undefined) return ''
  return first.number === last.number
    ? `Episode ${String(first.number)}`
    : `Episodes ${String(first.number)}–${String(last.number)}`
}

/**
 * Whether this device would play the copies a trip makes. A show with a
 * ready episode says which codecs its copies are in; otherwise the server's
 * default is checked.
 */
export function tripCodecsPlayable(
  anime: Pick<AnimeDetail, 'episodes'>,
  canPlayType: (type: string) => string,
): boolean {
  const named = anime.episodes.find((episode) => (episode.offline?.codecs ?? null) !== null)
  return codecsPlayable(named?.offline?.codecs ?? DEFAULT_COPY_CODECS, canPlayType)
}

/** The refusal codes `POST /api/anime/{id}/trip` answers with. */
export type TripRefusal =
  'demo_account' | 'trip_active' | 'storage_held' | 'count_out_of_range' | 'nothing_aired'

const REFUSALS: Record<TripRefusal, string> = {
  demo_account: 'The demo account cannot prepare trips.',
  trip_active: 'You already have a trip being prepared. Cancel it, or wait for it to finish.',
  storage_held:
    'Arc’s server is short of disk space right now, so it cannot start a trip. Try again later.',
  count_out_of_range: 'That is more episodes than this server allows for one trip.',
  nothing_aired: 'Nothing has aired after where you are in this show.',
}

/** The refusal code, when the error is one. */
export function tripRefusal(error: unknown): TripRefusal | null {
  const detail = errorDetail(error)
  return detail !== null && detail in REFUSALS ? (detail as TripRefusal) : null
}

/** A refused or failed trip request, as a sentence. */
export function tripErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not reach Arc. Try again when you are online.'
  const refusal = tripRefusal(error)
  if (refusal !== null) return REFUSALS[refusal]
  return 'Could not start the trip. Try again.'
}

/** A refused "Ask again", as a sentence. */
export function askAgainErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not reach Arc. Try again when you are online.'
  if (errorDetail(error) === 'trip_not_active') return 'This trip has ended.'
  return 'Could not ask for it again. Try again.'
}

/* --- Queries ------------------------------------------------------------- */

function fetchCurrentTrip(): Promise<Trip | null> {
  return apiFetch<Trip | null>('/api/trips/current').then((trip) => trip ?? null)
}

/**
 * `GET /api/trips/current`: the signed-in account's active trip, or null.
 * Asked every {@link TRIP_POLL_MS}, when Arc comes back on screen or back
 * online, and whenever the live stream says a copy or an episode moved.
 */
export function useCurrentTrip(enabled: boolean): UseQueryResult<Trip | null, Error> {
  return useQuery<Trip | null, Error>({
    queryKey: TRIP_QUERY_KEY,
    queryFn: fetchCurrentTrip,
    enabled,
    retry: false,
    staleTime: 0,
    refetchInterval: enabled ? TRIP_POLL_MS : false,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  })
}

function refreshAfterTrip(client: QueryClient, animeIds: readonly number[]): void {
  void client.invalidateQueries({ queryKey: TRIP_QUERY_KEY })
  for (const id of animeIds) void client.invalidateQueries({ queryKey: animeQueryKey(id) })
}

/** "Prepare for a trip": `POST /api/anime/{id}/trip {count}`. */
export function useCreateTrip(animeId: number): UseMutationResult<Trip, Error, number> {
  const client = useQueryClient()
  return useMutation<Trip, Error, number>({
    mutationFn: (count) =>
      apiFetch<Trip>(`/api/anime/${String(animeId)}/trip`, {
        method: 'POST',
        body: JSON.stringify({ count }),
      }),
    onSuccess: (trip) => {
      // The panel replaces the control at once; the refetch confirms it.
      client.setQueryData<AnimeDetail>(animeQueryKey(animeId), (current) =>
        current === undefined ? current : { ...current, trip },
      )
      client.setQueryData<Trip | null>(TRIP_QUERY_KEY, trip)
      refreshAfterTrip(client, [animeId])
    },
  })
}

export interface CancelTripInput {
  tripId: number
  animeId: number
}

/** "Cancel trip": `DELETE /api/trips/{id}`. What the device already holds stays. */
export function useCancelTrip(): UseMutationResult<null, Error, CancelTripInput> {
  const client = useQueryClient()
  return useMutation<null, Error, CancelTripInput>({
    mutationFn: ({ tripId }) =>
      apiFetch<null>(`/api/trips/${String(tripId)}`, { method: 'DELETE' }),
    onSuccess: (_result, { animeId }) => {
      client.setQueryData<AnimeDetail>(animeQueryKey(animeId), (current) =>
        current === undefined ? current : { ...current, trip: null },
      )
      client.setQueryData<Trip | null>(TRIP_QUERY_KEY, null)
      refreshAfterTrip(client, [animeId])
    },
  })
}

export interface AskAgainInput {
  tripId: number
  animeId: number
  episodeId: number
}

/** "Ask again" for an expired or released episode: `POST …/again` → the trip. */
export function useAskAgain(): UseMutationResult<Trip, Error, AskAgainInput> {
  const client = useQueryClient()
  return useMutation<Trip, Error, AskAgainInput>({
    mutationFn: ({ tripId, episodeId }) =>
      apiFetch<Trip>(`/api/trips/${String(tripId)}/episodes/${String(episodeId)}/again`, {
        method: 'POST',
      }),
    onSuccess: (trip, { animeId }) => {
      client.setQueryData<AnimeDetail>(animeQueryKey(animeId), (current) =>
        current === undefined ? current : { ...current, trip },
      )
      client.setQueryData<Trip | null>(TRIP_QUERY_KEY, trip)
      refreshAfterTrip(client, [animeId])
    },
  })
}

/* --- One trip episode, as this device sees it --------------------------- */

export interface TripRowStatus {
  /** "On this device · 98 MB", "Downloading 42%", "Making the small copy 30%", … */
  label: string
  tone: 'muted' | 'bright' | 'ok' | 'error'
  /** 0–100 while something is moving and the figure is known; else null. */
  percent: number | null
  /** Whether "Ask again" is offered: expired, or no longer on this device. */
  askAgain: boolean
}

function whole(fraction: number | null | undefined): number | null {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return null
  return Math.max(0, Math.min(100, Math.floor(fraction * 100)))
}

/**
 * The trip panel's line for one episode: the device's record first (it is
 * what the viewer will carry), else the server's phase. `declined` is an
 * episode this account took off the device by hand.
 */
export function tripRowStatus(
  episode: TripEpisode,
  record: DownloadRecord | undefined,
  declined: boolean,
  formatBytes: (bytes: number) => string,
): TripRowStatus {
  if (record !== undefined) {
    switch (record.state) {
      case 'downloaded':
        return {
          label: `On this device · ${formatBytes(record.bytes)}`,
          tone: 'ok',
          percent: null,
          askAgain: false,
        }
      case 'downloading': {
        const percent = record.total > 0 ? whole(record.bytes / record.total) : 0
        return {
          label: `Downloading ${String(percent ?? 0)}%`,
          tone: 'bright',
          percent,
          askAgain: false,
        }
      }
      case 'queued':
        return { label: 'Queued on this device', tone: 'muted', percent: null, askAgain: false }
      case 'preparing':
        return { label: 'Waiting for the copy', tone: 'muted', percent: null, askAgain: false }
      case 'paused':
        if (record.reason === 'quota') {
          return {
            label: `Paused · this ${deviceName()} is out of space`,
            tone: 'error',
            percent: null,
            askAgain: false,
          }
        }
        return {
          label: record.reason === 'by-hand' ? 'Paused' : 'Paused · carries on by itself',
          tone: 'muted',
          percent: null,
          askAgain: false,
        }
      case 'failed':
        return { label: 'Stopped on this device', tone: 'error', percent: null, askAgain: false }
    }
  }
  if (declined && episode.phase !== 'expired') {
    return { label: 'Removed from this device', tone: 'muted', percent: null, askAgain: true }
  }
  switch (episode.phase) {
    case 'available':
      return {
        label: `Ready to download${episode.size === null ? '' : ` · ${formatBytes(episode.size)}`}`,
        tone: 'bright',
        percent: null,
        askAgain: false,
      }
    case 'delivered':
      return { label: 'Not on this device', tone: 'muted', percent: null, askAgain: true }
    case 'expired':
      return { label: 'Expired on the server', tone: 'muted', percent: null, askAgain: true }
    case 'unavailable':
      return { label: PHASE_LABEL.unavailable, tone: 'error', percent: null, askAgain: false }
    case 'downloading':
    case 'preparing': {
      const percent = whole(episode.progress)
      return {
        label: `${PHASE_LABEL[episode.phase]}${percent === null ? '' : ` ${String(percent)}%`}`,
        tone: 'muted',
        percent,
        askAgain: false,
      }
    }
    case 'searching':
    case 'waiting_space':
      return { label: PHASE_LABEL[episode.phase], tone: 'muted', percent: null, askAgain: false }
  }
}

/** "3 of 12 on this device", the panel's and the Downloads group's headline count. */
export function onDeviceCount(trip: Trip, records: Downloads): number {
  return trip.episodes.filter((episode) => records[episode.episode_id]?.state === 'downloaded')
    .length
}
