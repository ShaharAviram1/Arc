/**
 * "Keep offline", and everything that can happen to it (spec FR-S9,
 * architecture §5.4d). Ported from Audiosey's `DownloadManager`.
 *
 * A store outside React — a download outlives the page that started it — read
 * through `useSyncExternalStore` (`useDownloads`).
 *
 * | state | what the viewer sees |
 * |---|---|
 * | (no record) | "Keep offline" |
 * | `preparing` | "Preparing on the server", with the server's percent when known |
 * | `queued` | waiting for the download ahead of it |
 * | `downloading` | a bar and `n %` |
 * | `paused` | why (`by-hand`, `interrupted`, `network`, `elsewhere`), and Resume |
 * | `downloaded` | the size, and a way to delete it |
 * | `failed` | why (`quota`, `auth`, `gone`, `size`, `error`, `evicted`, `unreadable`, `unprepared`), and Try again |
 *
 * **Which copy** (M19, owner 2026-10-05). Keep offline asks the server for its
 * *small copy* first (`POST /api/episodes/{id}/offline`): `available` → the
 * copy's `url` is downloaded (`variant: 'small'`); `queued` / `preparing` →
 * the record is `preparing` and the manager polls `GET …/offline` every
 * {@link POLL_MS} while the page is visible (and on every nudge) until it is
 * `available`; `failed` → a failed record whose Try again asks again; `409
 * source_gone` or `unavailable` → the full-size `download_url` (`variant:
 * 'full'`). A device whose `<video>` says it cannot play the copy's codecs
 * takes the full-size file too. A request with no response is `paused` /
 * `network` and asked again by the same ticker — never dropped. A record that
 * has not been given a URL yet (`url: null`) names no bytes on disk. Each copy
 * has its own file name (`opfs.ts`), so the two never touch each other.
 *
 * `paused` / `network` is a message, not a stop: the worker keeps retrying and
 * the next chunk that lands puts the record back to `downloading`.
 *
 * **When the device stops a write** (owner incident, 2026-10-07: a full iPad
 * fails every write with `InvalidStateError`, and so does a handle iPadOS
 * closed when Arc left the screen). The worker re-opens the file and tries
 * the chunk once; if that fails it ends the run `paused` / `interrupted` and
 * is *retired*: it is terminated before the next download, which runs on a
 * new one. The record is tried once more straight away on that new worker
 * (when Arc is on screen; otherwise it waits, paused, for the next nudge). A
 * second such stop with nothing written in between — or a
 * `QuotaExceededError` — is read as **the device being out of space**:
 * `paused` / `quota`, and **the queue stops** (nothing else starts a first
 * chunk only to fail the same way). Every nudge and every trip auto-keep
 * tick *probes*: the paused-for-space record is resumed alone, at the head of
 * the queue; once one of its chunks lands, the hold lifts and the queue flows.
 * `navigator.storage.estimate()` is never consulted for this — on iPadOS it
 * reports the browser's allowance, not the iPad's free space.
 *
 * **One download at a time**, in the order they were asked for.
 *
 * **Records belong to a user.** Each is keyed by (user, episode) and only the
 * signed-in owner's are in the snapshot; another account on the same iPad sees
 * none of them and cannot play them. The file is named by episode, so two
 * accounts that keep the same copy of an episode share its bytes, and it is
 * deleted only when the last record naming it goes.
 *
 * **A watched copy leaves by itself** (owner, 2026-10-08). Once the server
 * has accepted this account's completion of an episode — the one signal is
 * the outbox's `announceWatched(…, true)`: an online report at or past the
 * mark answered `completed`, the mark-watched call answered, or a replayed
 * completion came back `applied`; never a completion still queued — the
 * record is marked `watched`, and {@link DownloadManager.removeWatched} takes
 * it off the device through {@link DownloadManager.remove} (the file, the
 * record, a trip's release call). It runs at launch, on every trip auto-keep
 * tick, hourly, and after every flush of the outbox that reached the server;
 * never for the episode open in the player ({@link DownloadManager.enterPlayer}),
 * so a session that just completed waits until the player is left; never for
 * a record kept by hand (`keep`), with the device's switch off, or while a
 * mark or un-mark for the episode waits in the outbox. Any un-mark clears
 * `watched`.
 *
 * **A file is never touched while the worker may hold it.** From the moment a
 * pause is sent until the worker's terminal message for that file (which the
 * worker posts only after closing it) the name is *releasing*; a delete in
 * that window is deferred to the terminal message. A new download of a file no
 * record vouches for starts `fresh` — the worker truncates it first — so an
 * orphan is never resumed into, and the launch sweep removes any orphan left.
 */

import { apiFetch, ApiError, isUnreachable } from '@/lib/api'
import type { OfflineCopyOut } from '@/lib/anime'
import type { PlayInfo } from '@/lib/playback'
import { forgetCover, rememberCover } from '@/offline/cache'
import {
  isTerminal,
  type FailCode,
  type SuspendReason,
  type WorkerCommand,
  type WorkerMessage,
} from '@/offline/download'
import {
  blobUrlFor,
  fileNameFor,
  fileSize,
  listEpisodeFiles,
  deviceName,
  removeFile,
  requestPersistence,
  revokeCurrent,
  type CopyVariant,
} from '@/offline/opfs'
import { outbox } from '@/offline/outbox'
import { openStore, type KeyStore } from '@/offline/store'

export type DownloadState =
  'preparing' | 'queued' | 'downloading' | 'paused' | 'downloaded' | 'failed'

export type { CopyVariant }

/**
 * Why a record is paused: by the viewer; when Arc was closed, the account
 * changed or the device stopped a write (`interrupted`); waiting for a
 * connection; another window has it; or the device is out of space (`quota`,
 * which holds the queue until a probe's write lands).
 */
export type PauseReason = 'by-hand' | 'interrupted' | 'network' | 'elsewhere' | 'quota'

/** Failures the manager itself decides, beside the worker's. */
export type DeviceFailure = 'evicted' | 'unreadable' | 'unprepared'

export type DownloadReason = PauseReason | FailCode | DeviceFailure

/**
 * What is remembered about the episode so it renders and plays with no
 * network: the player's own payload at the moment the download started, minus
 * what only the server can know later (the resume position, the neighbours).
 */
export type EpisodeSnapshot = Pick<PlayInfo, 'anime' | 'episode' | 'duration'>

export interface DownloadRecord {
  userId: number
  episodeId: number
  animeId: number
  /** The OPFS file name. Derived from the episode id and the variant, never from input. */
  name: string
  /**
   * Where the bytes come from: the small copy's `url`, or the episode's
   * `download_url`. `null` while the server has not yet said where the small
   * copy is — such a record names no bytes on disk.
   */
  url: string | null
  /** Which copy this is (M19). Records from before it are `full`. */
  variant: CopyVariant
  /** The episode's `download_url`, kept so a small-copy wish can fall back to it. */
  fullUrl?: string | null
  /** The server's progress making the small copy, 0–1, while `preparing`; null when unknown. */
  serverProgress?: number | null
  /**
   * The trip this copy is kept for (M19 T6, FR-A12): set when the auto-keep
   * hook starts it, or adopts a record the device already had for one of the
   * trip's episodes. Its arrival is confirmed to the server, and its deletion
   * reported, under this trip.
   */
  tripId?: number
  /**
   * Whether the server has heard that this trip copy is on the device:
   * `pending` from the worker's `done` until `POST …/delivered` succeeds
   * (retried on launch, reconnect, return to the screen and every hook tick),
   * then `done`.
   */
  confirm?: 'pending' | 'done'
  /**
   * The server has accepted this account's completion of the episode (see
   * the class comment): the copy is removed at the next pass unless `keep`.
   * Cleared by an un-mark. Absent on records from before 2026-10-08.
   */
  watched?: boolean
  /** Kept by hand: never removed for being watched. Absent means false. */
  keep?: boolean
  /**
   * The small copy vanished from the server mid-download (a 404) and the
   * server was asked again by itself. Once per record until a download next
   * finishes, so a copy that keeps vanishing ends at "gone" instead of looping.
   */
  reasked?: boolean
  state: DownloadState
  /** Bytes on the device. */
  bytes: number
  /** Bytes the whole episode is, from `Content-Range`. `0` until the first chunk. */
  total: number
  etag: string | null
  reason: DownloadReason | null
  /** Why, in a sentence. */
  message: string | null
  /**
   * Set on a record paused `interrupted` because the device stopped a write
   * (not by the viewer, not by a relaunch): the next nudge, and the trip
   * auto-keep tick, resume it by themselves.
   */
  autoResume?: boolean
  /**
   * True until this record's own first chunk lands: whatever the file held
   * before is not vouched for, and the worker truncates it on the next run.
   */
  fresh?: boolean
  snapshot: EpisodeSnapshot
  created_at: string
  updated_at: string
}

/** The signed-in owner's records, by episode id. */
export type Downloads = Readonly<Record<number, DownloadRecord>>

export interface StartInput {
  episodeId: number
  /**
   * The `download_url` the show payload carried for the episode: the
   * fallback when the server cannot make a small copy.
   */
  url: string
}

/**
 * A trip episode whose small copy is waiting on the server (M19 T6): the
 * auto-keep hook hands it over. No `/offline` request is made — the server
 * answers that route for a `ready` episode only, and a trip-only episode is
 * never ready — the copy's own `url` is downloaded straight away.
 */
export interface TripCopyInput {
  episodeId: number
  tripId: number
  /** The copy's URL (`/media/{id}/offline.mp4`). */
  url: string
  /** The full-size `download_url`, when the episode is also ready; else none. */
  fullUrl?: string | null
}

/** What the manager needs of a worker, so a test can hand it a fake. */
export interface WorkerLike {
  postMessage(message: WorkerCommand): void
  onMessage(listener: (message: WorkerMessage) => void): void
  /** The worker failed to load, threw, or sent something unreadable. */
  onError(listener: (reason: string) => void): void
  terminate(): void
}

/** The quick retries pass in silence; after these the row says it is waiting. */
export const QUIET_RETRIES = 3

/** The device caveat, shown beside a running download and on the Downloads page. */
export const SCREEN_NOTE =
  'iPad and iPhone stop downloads while Arc is not on screen. Keep Arc open and it carries on.'

export const MESSAGES: Record<DownloadReason, string> = {
  'by-hand': 'Paused.',
  interrupted: 'Paused when Arc was closed or the account changed. Resume to carry on.',
  network: 'Waiting for a connection. It carries on by itself.',
  elsewhere: 'Downloading in another Arc window.',
  quota: `This ${deviceName()} is out of space — free some room and the download continues.`,
  auth: 'Stopped: you are signed out, or this account may not download.',
  gone: 'Stopped: the server no longer has this episode.',
  size: 'The download did not finish cleanly. Try again to fetch the rest.',
  error: 'The download stopped. Try again.',
  evicted: 'Removed by the device to free space — Keep offline again.',
  unreadable: 'This download would not play. Try again to download it afresh.',
  unprepared: 'Arc could not make the smaller copy for this device. Try again.',
}

/** Which copy is on the device, in words: the control's menu (M19). */
export const COPY_LABEL: Record<CopyVariant, string> = {
  small: 'Smaller copy for this device',
  full: 'Full-size copy',
}

/** The same, quietly, beside the size on the Downloads page. */
export const COPY_NOTE: Record<CopyVariant, string> = {
  small: 'smaller copy',
  full: 'full size',
}

/**
 * A Downloads row's word on what happens to it once watched (owner,
 * 2026-10-08); `null` when there is nothing to say (the switch is off, or the
 * episode is neither kept nor watched).
 */
export function watchedNote(record: DownloadRecord, removeWatched: boolean): string | null {
  if (!removeWatched) return null
  if (record.keep === true) return 'Kept'
  if (record.watched === true) return 'Watched · removing soon'
  return null
}

/** How often a `preparing` record asks the server, while the page is on screen. */
export const POLL_MS = 20_000

/**
 * Whether this device's `<video>` can play a copy with these RFC 6381 codecs
 * (`avc1.640028`, `hvc1.1.6.L93.B0`). An empty answer from `canPlayType` is a
 * "no"; `maybe` and `probably` are yeses. A copy that names no codecs is
 * taken on trust.
 */
export function codecsPlayable(
  codecs: string | null,
  canPlayType: (type: string) => string,
): boolean {
  if (codecs === null || codecs.trim() === '') return true
  return canPlayType(`video/mp4; codecs="${codecs.trim()}"`) !== ''
}

/** This device's `<video>.canPlayType`; `''` (no) where there is no DOM. */
export function canPlayTypeHere(type: string): string {
  return defaultCanPlayType(type)
}

function defaultCanPlayType(type: string): string {
  try {
    return document.createElement('video').canPlayType(type)
  } catch {
    return ''
  }
}

function defaultRequestCopy(episodeId: number): Promise<OfflineCopyOut> {
  return apiFetch<OfflineCopyOut>(`/api/episodes/${String(episodeId)}/offline`, {
    method: 'POST',
  })
}

function defaultPollCopy(episodeId: number): Promise<OfflineCopyOut> {
  return apiFetch<OfflineCopyOut>(`/api/episodes/${String(episodeId)}/offline`)
}

function defaultConfirmDelivered(
  tripId: number,
  episodeId: number,
  etag: string | null,
): Promise<unknown> {
  return apiFetch<null>(`/api/trips/${String(tripId)}/episodes/${String(episodeId)}/delivered`, {
    method: 'POST',
    body: JSON.stringify({ etag }),
  })
}

function defaultReleaseDelivered(tripId: number, episodeId: number): Promise<unknown> {
  return apiFetch<null>(`/api/trips/${String(tripId)}/episodes/${String(episodeId)}/delivered`, {
    method: 'DELETE',
  })
}

/**
 * A confirmation the server answered with a final no: the trip or the episode
 * is not this account's (404), or this account may not keep copies (403).
 * Nothing more can be told, so the record stops asking.
 */
function confirmIsFinal(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403)
}

/** The note that says this account took a trip episode off the device by hand. */
function declineKey(userId: number, tripId: number, episodeId: number): string {
  return `${DECLINE_PREFIX}${String(userId)}:${String(tripId)}:${String(episodeId)}`
}

const DECLINE_PREFIX = 'trip-skip:'

/** The device's "Remove episodes once watched" switch, in IndexedDB `player`. Absent means on. */
export const REMOVE_WATCHED_KEY = 'remove-watched'

/** How often watched copies are looked for while Arc is open, beside the other triggers. */
export const REMOVE_WATCHED_MS = 60 * 60_000

/** Whether a mark or un-mark for this episode still waits in the outbox, from its own store. */
async function defaultQueuedFor(userId: number, episodeId: number): Promise<boolean> {
  const records = await outbox().all()
  return records.some(
    (record) =>
      record.user_id === userId &&
      record.episode_id === episodeId &&
      record.problem === undefined &&
      record.kind !== 'position',
  )
}

function defaultIsVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden'
}

/**
 * The refusals after which the device takes the full-size file instead: the
 * server no longer has the source to make a copy from (409 `source_gone`), is
 * short of disk (409 `storage_held`), or has too many copies waiting already
 * (429 `copy_queue_full`).
 */
function takesFullInstead(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  const body = error.body
  const detail =
    typeof body === 'object' && body !== null ? (body as { detail?: unknown }).detail : undefined
  if (error.status === 409) return detail === 'source_gone' || detail === 'storage_held'
  if (error.status === 429) return detail === 'copy_queue_full'
  return false
}

/** A record the server has not yet given a URL: it names no bytes on disk. */
function holdsNoFile(record: DownloadRecord): boolean {
  return record.url === null
}

/**
 * A trip record with no URL yet waits for the auto-keep hook, not for the
 * `/offline` routes: those answer for a `ready` episode only, and a trip-only
 * episode never is (they would say 404, which is not "gone" here).
 */
function waitsForTrip(record: DownloadRecord): boolean {
  return record.tripId !== undefined && record.url === null
}

/** A record waiting on the server: being made, or a request that found no network. */
function awaitingServer(record: DownloadRecord): boolean {
  return (
    record.state === 'preparing' ||
    (record.state === 'paused' && record.reason === 'network' && record.url === null)
  )
}

/** A record from before M19 (no `variant`) is the full-size file. */
function normalise(record: DownloadRecord): DownloadRecord {
  const variant: CopyVariant = record.variant === 'small' ? 'small' : 'full'
  return record.variant === variant ? record : { ...record, variant }
}

/** The device stopped a write while Arc was off screen (owner incident, 2026-10-07). */
export const BACKGROUNDED =
  'Arc was put in the background while downloading; it resumes when Arc is back on screen.'

/**
 * Stops a record may have in a row, with nothing written between them, before
 * Arc reads them as the device being out of space: one on the worker that
 * hit it (after its own re-open and retry), one more on a new worker.
 */
export const STRIKES_FOR_SPACE = 2

const RESTARTED = 'Arc re-encoded this episode, so the download started again.'
const WORKER_FAILED = 'Arc could not run its downloader on this device.'

function keyOf(userId: number, episodeId: number): string {
  return `${String(userId)}:${String(episodeId)}`
}

function keyOfRecord(record: DownloadRecord): string {
  return keyOf(record.userId, record.episodeId)
}

interface WakeLockSentinelLike {
  release: () => Promise<void>
  addEventListener?: (type: 'release', listener: () => void) => void
}

export interface ManagerOptions {
  store?: KeyStore
  createWorker?: () => WorkerLike
  /** What is already on disk. Injected so tests need no OPFS. */
  sizeOf?: (name: string) => Promise<number>
  removeFrom?: (name: string) => Promise<void>
  /** Every episode file on disk, for the launch sweep. */
  listFiles?: () => Promise<string[]>
  /** Mints the playable URL. Injected so tests need no OPFS and no blob URLs. */
  blobUrl?: (name: string) => Promise<string | null>
  /** Revokes the live blob URL. */
  revokeUrl?: () => void
  persist?: () => Promise<boolean>
  /** The player payload for an episode — what is remembered for offline. */
  loadInfo?: (episodeId: number) => Promise<PlayInfo>
  keepCover?: (userId: number, animeId: number, url: string | null) => Promise<void>
  dropCover?: (userId: number, animeId: number) => Promise<void>
  now?: () => number
  /** `POST /api/episodes/{id}/offline`: ask for the small copy. */
  requestCopy?: (episodeId: number) => Promise<OfflineCopyOut>
  /** `GET /api/episodes/{id}/offline`: how the small copy is getting on. */
  pollCopy?: (episodeId: number) => Promise<OfflineCopyOut>
  /** `HTMLMediaElement.canPlayType`, for the codec check. */
  canPlayType?: (type: string) => string
  /** Whether the page is on screen; polling waits while it is not. */
  isVisible?: () => boolean
  /** `POST /api/trips/{id}/episodes/{eid}/delivered {etag}` (M19 T6). */
  confirmDelivered?: (tripId: number, episodeId: number, etag: string | null) => Promise<unknown>
  /** `DELETE /api/trips/{id}/episodes/{eid}/delivered`: the device deleted its copy. */
  releaseDelivered?: (tripId: number, episodeId: number) => Promise<unknown>
  /**
   * Where the trip episodes taken off the device by hand are remembered, so
   * the auto-keep hook does not fetch them again (IndexedDB `player`).
   */
  notes?: KeyStore
  /**
   * Whether a mark or un-mark of this episode by this account still waits in
   * the outbox: a watched copy is not removed until it has synced.
   */
  queuedFor?: (userId: number, episodeId: number) => Promise<boolean>
}

function defaultWorker(): WorkerLike {
  const worker = new Worker(new URL('./downloadWorker.ts', import.meta.url), { type: 'module' })
  return {
    postMessage: (message) => {
      worker.postMessage(message)
    },
    onMessage: (listener) => {
      worker.addEventListener('message', (event: MessageEvent<WorkerMessage>) => {
        listener(event.data)
      })
    },
    onError: (listener) => {
      worker.addEventListener('error', (event: ErrorEvent) => {
        event.preventDefault()
        listener(event.message === '' ? 'the worker failed to load' : event.message)
      })
      worker.addEventListener('messageerror', () => {
        listener('the worker sent a message Arc could not read')
      })
    },
    terminate: () => {
      worker.terminate()
    },
  }
}

function defaultLoadInfo(episodeId: number): Promise<PlayInfo> {
  return apiFetch<PlayInfo>(`/api/episodes/${String(episodeId)}/play`)
}

const EMPTY: Downloads = Object.freeze({})

/**
 * The downloaded episodes of the same show either side of `record`, by
 * episode number — what the player offers as previous / next with no network.
 */
export function downloadedNeighbours(
  records: Downloads,
  record: DownloadRecord,
): { previous: DownloadRecord | null; next: DownloadRecord | null } {
  const siblings = Object.values(records)
    .filter((other) => other.animeId === record.animeId && other.state === 'downloaded')
    .sort((a, b) => a.snapshot.episode.number - b.snapshot.episode.number)
  const number = record.snapshot.episode.number
  let previous: DownloadRecord | null = null
  let next: DownloadRecord | null = null
  for (const other of siblings) {
    const n = other.snapshot.episode.number
    if (n < number) previous = other
    else if (n > number && next === null) next = other
  }
  return { previous, next }
}

export class DownloadManager {
  private readonly store: KeyStore
  private readonly createWorker: () => WorkerLike
  private readonly sizeOf: (name: string) => Promise<number>
  private readonly removeFrom: (name: string) => Promise<void>
  private readonly listFiles: () => Promise<string[]>
  private readonly blobUrl: (name: string) => Promise<string | null>
  private readonly revokeUrl: () => void
  private readonly persist: () => Promise<boolean>
  private readonly loadInfo: (episodeId: number) => Promise<PlayInfo>
  private readonly keepCover: (userId: number, animeId: number, url: string | null) => Promise<void>
  private readonly dropCover: (userId: number, animeId: number) => Promise<void>
  private readonly now: () => number
  private readonly requestCopy: (episodeId: number) => Promise<OfflineCopyOut>
  private readonly pollCopy: (episodeId: number) => Promise<OfflineCopyOut>
  private readonly canPlayType: (type: string) => string
  private readonly isVisible: () => boolean
  private readonly confirmDelivered: (
    tripId: number,
    episodeId: number,
    etag: string | null,
  ) => Promise<unknown>
  private readonly releaseDelivered: (tripId: number, episodeId: number) => Promise<unknown>
  private readonly notes: KeyStore
  private readonly queuedFor: (userId: number, episodeId: number) => Promise<boolean>

  /** "Remove episodes once watched", this device's switch. On until it is turned off. */
  private removeWatchedOn = true
  /** Episodes open in the player, with how many players hold each. Never removed for being watched. */
  private playing = new Map<number, number>()
  /** Watched marks applied in the order they were announced. */
  private watchedChain: Promise<void> = Promise.resolve()
  /** A pass of {@link removeWatched} in flight, so two triggers make one. */
  private removing: Promise<void> | null = null

  private worker: WorkerLike | null = null
  /**
   * The worker reported a write it could not make (a closed handle, a full
   * disk): it is terminated before the next download is handed out, and that
   * download runs on a new one.
   */
  private retired = false
  /** Device-stopped runs in a row per key, with no chunk landing between. See {@link STRIKES_FOR_SPACE}. */
  private strikes = new Map<string, number>()
  /** The paused-for-space record being tried again, alone, while the queue is held. */
  private probe: string | null = null
  /** Every user's records on this device, by `user:episode`. */
  private all = new Map<string, DownloadRecord>()
  private snapshot: Downloads = EMPTY
  private owner: number | null = null
  private listeners = new Set<() => void>()
  /** The record the worker is writing, by key, and the run it is on. */
  private active: string | null = null
  private activeRun: number | null = null
  private runs = 0
  /** Keys waiting their turn, in the order asked for. */
  private queue: string[] = []
  /** File names the worker may still hold open: a pause sent, no terminal message yet. */
  private releasing = new Set<string>()
  /** Files to delete once the worker has let go of them. */
  private doomed = new Set<string>()
  /** The one live blob URL, by key. See {@link playableUrlNow}. */
  private urls = new Map<string, string>()
  private wakeLock: WakeLockSentinelLike | null = null
  private wantWakeLock = false
  private hydrated: Promise<void> | null = null
  /** Episodes whose payload is being fetched, so a double tap starts one download. */
  private starting = new Set<string>()
  /** The server request in flight per key, by a token; a pause or delete forgets it. */
  private asking = new Map<string, number>()
  private asks = 0
  private ticker: ReturnType<typeof setInterval> | null = null
  /** Trip confirmations in flight, by key, so a tick does not send a second. */
  private confirming = new Set<string>()
  /** Trip episodes taken off the device by hand: `trip-skip:<user>:<trip>:<episode>`. */
  private declined = new Set<string>()

  constructor(options: ManagerOptions = {}) {
    this.store = options.store ?? openStore('downloads')
    this.createWorker = options.createWorker ?? defaultWorker
    this.sizeOf = options.sizeOf ?? fileSize
    this.removeFrom = options.removeFrom ?? removeFile
    this.listFiles = options.listFiles ?? listEpisodeFiles
    this.blobUrl = options.blobUrl ?? blobUrlFor
    this.revokeUrl = options.revokeUrl ?? revokeCurrent
    this.persist = options.persist ?? requestPersistence
    this.loadInfo = options.loadInfo ?? defaultLoadInfo
    this.keepCover =
      options.keepCover ?? ((userId, animeId, url) => rememberCover(userId, animeId, url))
    this.dropCover = options.dropCover ?? forgetCover
    this.now = options.now ?? (() => Date.now())
    this.requestCopy = options.requestCopy ?? defaultRequestCopy
    this.pollCopy = options.pollCopy ?? defaultPollCopy
    this.canPlayType = options.canPlayType ?? defaultCanPlayType
    this.isVisible = options.isVisible ?? defaultIsVisible
    this.confirmDelivered = options.confirmDelivered ?? defaultConfirmDelivered
    this.releaseDelivered = options.releaseDelivered ?? defaultReleaseDelivered
    this.notes = options.notes ?? openStore('player')
    this.queuedFor = options.queuedFor ?? defaultQueuedFor
  }

  /* --- Store plumbing ----------------------------------------------------- */

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getSnapshot = (): Downloads => this.snapshot

  private emit(): void {
    const owner = this.owner
    const next: Record<number, DownloadRecord> = {}
    if (owner !== null) {
      for (const record of this.all.values()) {
        if (record.userId === owner) next[record.episodeId] = record
      }
    }
    this.snapshot = next
    this.keepPolling()
    for (const listener of this.listeners) listener()
  }

  private stamp(): string {
    return new Date(this.now()).toISOString()
  }

  private write(record: DownloadRecord): void {
    this.all.set(keyOfRecord(record), record)
    this.emit()
    void this.store.put(keyOfRecord(record), record)
  }

  private patch(key: string, patch: Partial<DownloadRecord>): DownloadRecord | null {
    const existing = this.all.get(key)
    if (existing === undefined) return null
    const next = { ...existing, ...patch, updated_at: this.stamp() }
    this.write(next)
    return next
  }

  /* --- Who is looking --------------------------------------------------------- */

  /**
   * Point the manager at the signed-in account (`null` when nobody is: a
   * sign-out). Anything running or queued for somebody else stops and waits,
   * paused, for its owner; the live blob URL is revoked. When an account comes
   * back, its downloads that failed for want of a session carry on by
   * themselves.
   */
  setOwner(userId: number | null): void {
    if (this.owner === userId) return
    this.owner = userId
    this.urls.clear()
    this.revokeUrl()
    const active = this.active === null ? undefined : this.all.get(this.active)
    if (active !== undefined && active.userId !== userId) this.halt(active)
    for (const [key, record] of this.all) {
      if (record.userId === userId) continue
      if (record.state === 'queued' || record.state === 'downloading') {
        this.all.set(key, {
          ...record,
          state: 'paused',
          reason: 'interrupted',
          message: MESSAGES.interrupted,
          autoResume: false,
          updated_at: this.stamp(),
        })
        void this.store.put(key, this.all.get(key))
      }
    }
    this.queue = this.queue.filter((key) => this.all.get(key)?.userId === userId)
    this.emit()
    if (userId !== null) {
      for (const record of Object.values(this.snapshot)) {
        if (record.state === 'failed' && record.reason === 'auth') this.resume(record.episodeId)
      }
      // Whatever of this account waits on the server asks now.
      this.poll()
    }
    this.pump()
  }

  get ownerId(): number | null {
    return this.owner
  }

  /* --- Launch --------------------------------------------------------------- */

  /**
   * Read what is on the device back into memory, checking it against the
   * disk, and sweep any episode file no record names.
   */
  hydrate(): Promise<void> {
    this.hydrated ??= this.reconcile()
    return this.hydrated
  }

  /** Resolves once {@link hydrate} has run. Never rejects. */
  whenHydrated(): Promise<void> {
    return this.hydrate()
  }

  private async reconcile(): Promise<void> {
    try {
      for (const [key, value] of await this.notes.entries<unknown>()) {
        if (key.startsWith(DECLINE_PREFIX) && value === true) this.declined.add(key)
        if (key === REMOVE_WATCHED_KEY) this.removeWatchedOn = value !== false
      }
    } catch {
      // Nothing remembered: the hook may offer a removed trip episode again.
    }
    const entries = await this.store.entries<DownloadRecord>()
    for (const [key, stored] of entries) {
      if (typeof stored !== 'object' || stored === null) continue
      const record = normalise(stored)
      // A record with no URL yet names no bytes; whatever is on disk under its
      // name is somebody else's, or an orphan.
      const bytes = holdsNoFile(record) ? 0 : await this.sizeOf(record.name)
      let next: DownloadRecord
      if (record.state === 'preparing') {
        // The server carries on making it whether Arc is open or not; the
        // poll picks it up again.
        next = { ...record, bytes: 0 }
      } else if (record.total > 0 && bytes === record.total) {
        next = { ...record, bytes, state: 'downloaded', reason: null, message: null }
      } else if (record.state === 'downloaded') {
        // The device took it back (eviction, or storage cleared). Said, not
        // hidden: the row offers Keep offline again, and Delete.
        next = {
          ...record,
          bytes,
          state: 'failed',
          reason: 'evicted',
          message: MESSAGES.evicted,
          fresh: true,
        }
      } else if (record.state === 'failed' && record.reason === 'quota') {
        // Before 2026-10-07 a full device was a failure; it is a pause now.
        next = { ...record, bytes, state: 'paused', message: MESSAGES.quota }
      } else if (record.state === 'failed') {
        next = { ...record, bytes }
      } else {
        const reason: PauseReason =
          record.reason === 'by-hand' || record.reason === 'quota' ? record.reason : 'interrupted'
        next = { ...record, bytes, state: 'paused', reason, message: MESSAGES[reason] }
      }
      // A launch-time record may have been written while this tab was
      // starting one of its own; the in-memory one is newer.
      if (!this.all.has(key)) this.all.set(key, next)
      void this.store.put(key, this.all.get(key))
    }
    await this.sweep()
    this.emit()
    // A download that stopped because the session had lapsed carries on now
    // that somebody is signed in again (the same rule as `setOwner`).
    for (const record of Object.values(this.snapshot)) {
      if (record.state === 'failed' && record.reason === 'auth') this.resume(record.episodeId)
    }
    this.poll()
    this.confirmPending()
    await this.passWatched()
  }

  /** Remove every episode file no record names: the orphans of an interrupted delete. */
  private async sweep(): Promise<void> {
    const named = new Set(
      [...this.all.values()].filter((record) => !holdsNoFile(record)).map((record) => record.name),
    )
    for (const name of await this.listFiles()) {
      if (!named.has(name) && !this.releasing.has(name)) await this.removeFrom(name)
    }
  }

  /* --- Reading ---------------------------------------------------------------- */

  record(episodeId: number): DownloadRecord | undefined {
    return this.snapshot[episodeId]
  }

  /** Whether this episode plays from the device for the signed-in account. */
  isOnDevice(episodeId: number): boolean {
    return this.snapshot[episodeId]?.state === 'downloaded'
  }

  /**
   * A blob URL for a downloaded episode, or `null` to stream it instead.
   * Memoised, and the memo holds **one** episode: `blobUrlFor` revokes the
   * URL it minted last, so an older entry would name a dead URL.
   */
  async playableUrl(episodeId: number): Promise<string | null> {
    const record = this.snapshot[episodeId]
    if (record === undefined || record.state !== 'downloaded') return null
    const key = keyOfRecord(record)
    const known = this.urls.get(key)
    if (known !== undefined) return known
    const url = await this.blobUrl(record.name)
    this.urls.clear()
    if (url !== null) this.urls.set(key, url)
    return url
  }

  /**
   * The blob URL already minted, with no `await` in the way — for code that
   * runs inside a tap, where an `await` before `play()` costs the user
   * gesture on iOS.
   */
  playableUrlNow(episodeId: number): string | null {
    const record = this.snapshot[episodeId]
    if (record === undefined || record.state !== 'downloaded') return null
    return this.urls.get(keyOfRecord(record)) ?? null
  }

  /**
   * The element would not play the file: the record becomes `failed` /
   * `unreadable`, so the row offers Try again (a fresh download) instead of
   * minting the same URL over the same bytes.
   */
  markUnplayable(episodeId: number): void {
    const record = this.snapshot[episodeId]
    if (record === undefined) return
    const key = keyOfRecord(record)
    this.urls.delete(key)
    this.revokeUrl()
    this.patch(key, {
      state: 'failed',
      reason: 'unreadable',
      message: MESSAGES.unreadable,
      fresh: true,
    })
  }

  /* --- Doing ------------------------------------------------------------------ */

  /**
   * Start keeping an episode on this device. Needs the network (for its
   * payload, and to ask the server for its small copy).
   *
   * Rejects only when the episode's payload cannot be had; everything after
   * that — the server's answer, or no answer — lands on the record.
   */
  async start(input: StartInput): Promise<void> {
    // A tap that lands before the launch-time reconcile must not race it.
    await this.hydrate()
    const owner = this.owner
    if (owner === null) return
    const key = keyOf(owner, input.episodeId)
    const existing = this.all.get(key)
    if (existing !== undefined) {
      if (existing.state !== 'downloaded') this.resume(input.episodeId)
      return
    }
    if (this.starting.has(key)) return
    this.starting.add(key)
    try {
      const info = await this.loadInfo(input.episodeId)
      if (this.owner !== owner || this.all.has(key)) return
      const at = this.stamp()
      this.write({
        userId: owner,
        episodeId: input.episodeId,
        animeId: info.anime.id,
        name: fileNameFor(input.episodeId, 'small'),
        // Nothing to download until the server says where the copy is.
        url: null,
        variant: 'small',
        fullUrl: input.url,
        state: 'preparing',
        serverProgress: null,
        bytes: 0,
        total: 0,
        etag: null,
        fresh: true,
        reason: null,
        message: null,
        snapshot: { anime: info.anime, episode: info.episode, duration: info.duration },
        created_at: at,
        updated_at: at,
      })
      // Asked on the first download the viewer actually starts.
      void this.persist()
      void this.keepCover(owner, info.anime.id, info.anime.cover_large_url ?? info.anime.cover_url)
    } finally {
      this.starting.delete(key)
    }
    await this.ask(key, 'request')
  }

  pause(episodeId: number): void {
    const record = this.snapshot[episodeId]
    if (record === undefined || record.state === 'downloaded') return
    const key = keyOfRecord(record)
    if (this.active === key) this.halt(record)
    this.queue = this.queue.filter((queued) => queued !== key)
    // Stop waiting on the server: the server keeps or expires its copy itself.
    this.asking.delete(key)
    if (this.probe === key) this.probe = null
    this.patch(key, {
      state: 'paused',
      reason: 'by-hand',
      message: MESSAGES['by-hand'],
      autoResume: false,
    })
    this.pump()
  }

  /**
   * Resume a paused download, or try a failed one again. A record the server
   * has not given a URL yet — and a small copy the server no longer has —
   * asks the server again instead.
   */
  resume(episodeId: number): void {
    const record = this.snapshot[episodeId]
    if (record === undefined || record.state === 'downloaded') return
    const key = keyOfRecord(record)
    if (record.state === 'preparing') {
      if (!this.asking.has(key)) void this.ask(key, 'poll')
      return
    }
    const askAgain =
      record.url === null ||
      (record.variant === 'small' && record.state === 'failed' && record.reason === 'gone')
    if (askAgain && this.active !== key) {
      this.queue = this.queue.filter((queued) => queued !== key)
      this.patch(key, {
        state: 'preparing',
        url: null,
        serverProgress: null,
        reason: null,
        message: null,
      })
      void this.ask(key, 'request')
      return
    }
    if (this.active === key) {
      this.nudge()
      return
    }
    if (record.reason === 'quota') {
      // The probe: tried alone, ahead of everything the space hold kept back.
      this.probe = key
      this.queue = [key, ...this.queue.filter((queued) => queued !== key)]
    } else if (!this.queue.includes(key)) {
      this.queue.push(key)
    }
    this.patch(key, { state: 'queued', reason: null, message: null, autoResume: false })
    this.pump()
  }

  /**
   * Resume what the device stopped, never what the viewer paused: a record
   * paused `interrupted` by a stopped write, and one paused-for-space record
   * as the probe (its write is the only honest test of free space on iPadOS).
   * Run on every nudge and every trip auto-keep tick; nothing while Arc is
   * off screen, where a write would only be stopped again.
   */
  resumeSuspended(): void {
    if (!this.isVisible()) return
    let probing = this.probe !== null
    for (const record of Object.values(this.snapshot)) {
      if (record.state !== 'paused') continue
      if (keyOfRecord(record) === this.active) continue
      if (record.reason === 'interrupted' && record.autoResume === true) {
        this.resume(record.episodeId)
      } else if (record.reason === 'quota' && !probing) {
        probing = true
        this.resume(record.episodeId)
      }
    }
  }

  /** Take an episode off this device. The server's copy is untouched. */
  async remove(episodeId: number): Promise<void> {
    const record = this.snapshot[episodeId]
    if (record === undefined) return
    const key = keyOfRecord(record)
    this.queue = this.queue.filter((queued) => queued !== key)
    this.asking.delete(key)
    this.urls.delete(key)
    this.all.delete(key)
    if (record.tripId !== undefined) {
      // A trip episode taken off the device by hand: the server stops counting
      // it as held (best effort — a lost call only means the window keeps
      // skipping it until the trip ends), and the auto-keep hook leaves it
      // alone until "Ask again".
      const declined = declineKey(record.userId, record.tripId, record.episodeId)
      this.declined.add(declined)
      void this.notes.put(declined, true).catch(() => undefined)
      void this.releaseDelivered(record.tripId, record.episodeId).catch(() => undefined)
    }
    await this.store.delete(key)
    this.emit()
    if (this.active === key) this.halt(record)
    if (this.sharedRecord(record.name) === undefined) {
      if (this.releasing.has(record.name)) {
        // The worker still holds the file; it goes when the worker says it
        // has closed it.
        this.doomed.add(record.name)
      } else {
        await this.removeFrom(record.name)
      }
    }
    const stillShown = [...this.all.values()].some(
      (other) => other.userId === record.userId && other.animeId === record.animeId,
    )
    if (!stillShown) await this.dropCover(record.userId, record.animeId)
    this.pump()
  }

  /** The app is on screen again, or back online: carry on with whatever was running. */
  nudge(): void {
    // Whatever is waiting on the server asks now rather than at the next tick.
    this.poll()
    this.confirmPending()
    const key = this.active
    const active = key === null ? undefined : this.all.get(key)
    if (key !== null && active !== undefined && active.url !== null) {
      if (active.state === 'paused' && active.reason === 'network') {
        this.patch(key, { state: 'downloading', reason: null, message: null })
      }
      this.send(this.commandFor(active, active.url))
    } else {
      // A download another window was doing may be free by now.
      for (const record of Object.values(this.snapshot)) {
        if (record.state === 'paused' && record.reason === 'elsewhere') {
          this.resume(record.episodeId)
        }
      }
    }
    // What the device stopped carries on (and a space hold is probed).
    this.resumeSuspended()
    this.pump()
  }

  /** The automatic recoveries. Returns a function that unwires them. */
  listen(): () => void {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      // The browser drops a screen wake lock whenever the page is hidden.
      if (this.wantWakeLock) void this.holdWakeLock()
      this.nudge()
    }
    const onOnline = () => {
      this.nudge()
    }
    const hourly = setInterval(() => {
      void this.removeWatched()
    }, REMOVE_WATCHED_MS)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onOnline)
    return () => {
      clearInterval(hourly)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('online', onOnline)
    }
  }

  /* --- The server's small copy ------------------------------------------------- */

  /**
   * Poll while anything of the signed-in account waits on the server, and not
   * otherwise. Run on every change ({@link emit}), so it can never be left
   * ticking for a record that is gone, or idle for one that waits.
   */
  private keepPolling(): void {
    const owner = this.owner
    const waiting =
      owner !== null &&
      [...this.all.values()].some(
        (record) => record.userId === owner && awaitingServer(record) && !waitsForTrip(record),
      )
    if (waiting && this.ticker === null) {
      this.ticker = setInterval(() => {
        if (this.isVisible()) this.poll()
      }, POLL_MS)
    } else if (!waiting && this.ticker !== null) {
      clearInterval(this.ticker)
      this.ticker = null
    }
  }

  /** Ask the server about every record of the signed-in account that waits on it. */
  private poll(): void {
    for (const record of Object.values(this.snapshot)) {
      if (!awaitingServer(record) || waitsForTrip(record)) continue
      const key = keyOfRecord(record)
      if (this.asking.has(key)) continue
      void this.ask(key, record.state === 'preparing' ? 'poll' : 'request')
    }
  }

  /**
   * One request to the server about a record's small copy — `POST` to ask for
   * it, `GET` to see how it is getting on — and what its answer means. Never
   * rejects. An answer that arrives after a pause, a delete or a change of
   * account is dropped.
   */
  private async ask(key: string, how: 'request' | 'poll'): Promise<void> {
    const record = this.all.get(key)
    if (record === undefined) return
    this.asks += 1
    const token = this.asks
    this.asking.set(key, token)
    let answer: OfflineCopyOut | null = null
    let refusal: unknown = null
    try {
      const found = await (how === 'request'
        ? this.requestCopy(record.episodeId)
        : this.pollCopy(record.episodeId))
      if (typeof found === 'object' && found !== null) answer = found
      else refusal = new Error('the server sent no answer')
    } catch (error) {
      refusal = error
    }
    if (this.asking.get(key) !== token) return
    this.asking.delete(key)
    const current = this.all.get(key)
    if (current === undefined || current.userId !== this.owner || !awaitingServer(current)) return
    try {
      if (answer === null) await this.refused(key, current, refusal, how)
      else await this.answered(key, answer, how)
    } catch (error) {
      this.fail(key, 'error', `${MESSAGES.error} (${String(error)})`)
    }
  }

  private async answered(
    key: string,
    copy: OfflineCopyOut,
    how: 'request' | 'poll',
  ): Promise<void> {
    // A copy this device cannot play is no use however small: the full file.
    if (!codecsPlayable(copy.codecs, this.canPlayType)) {
      await this.place(key, 'full')
      return
    }
    switch (copy.state) {
      case 'available':
        await (copy.url === null ? this.place(key, 'full') : this.place(key, 'small', copy.url))
        return
      case 'queued':
      case 'preparing':
        this.patch(key, {
          state: 'preparing',
          serverProgress: copy.progress,
          reason: null,
          message: null,
        })
        return
      case 'none':
        // Nothing on the server (the wish expired there): ask for it again.
        this.patch(key, { state: 'preparing', serverProgress: null, reason: null, message: null })
        if (how === 'poll') await this.ask(key, 'request')
        return
      case 'failed':
        this.patch(key, {
          state: 'failed',
          url: null,
          serverProgress: null,
          reason: 'unprepared',
          message: MESSAGES.unprepared,
        })
        return
      case 'unavailable':
        await this.place(key, 'full')
        return
    }
  }

  private async refused(
    key: string,
    record: DownloadRecord,
    error: unknown,
    how: 'request' | 'poll',
  ): Promise<void> {
    if (takesFullInstead(error)) {
      await this.place(key, 'full')
      return
    }
    if (error instanceof ApiError && !isUnreachable(error)) {
      if (error.status === 401 || error.status === 403) {
        this.fail(key, 'auth')
      } else if (error.status === 404 && record.tripId !== undefined) {
        // A trip-only episode: the hook hands over its copy's URL when the
        // trip says it is available.
        this.patch(key, { state: 'preparing', url: null, reason: null, message: null })
      } else if (error.status === 404) {
        this.fail(key, 'gone')
      } else if (how === 'request') {
        this.fail(key, 'error', `${MESSAGES.error} (${error.message})`)
      }
      // A poll the server answered oddly: the next tick asks again.
      return
    }
    // No answer at all. A poll just waits for the next tick; a request says
    // it is waiting for a connection, and the ticker asks again.
    if (how === 'request' && record.state !== 'paused') {
      this.patch(key, {
        state: 'paused',
        url: null,
        serverProgress: null,
        reason: 'network',
        message: MESSAGES.network,
      })
    }
  }

  private fail(key: string, reason: FailCode | DeviceFailure, message?: string): void {
    this.patch(key, {
      state: 'failed',
      serverProgress: null,
      reason,
      message: message ?? MESSAGES[reason],
    })
  }

  /**
   * Queue the download of `variant`: the small copy from the URL the server
   * gave, or the full-size file from the episode's `download_url`. The same
   * shared-file rule as ever, per file name: another account's copy *of the
   * same variant* is resumed or confirmed; anything else on disk starts over.
   */
  private async place(key: string, variant: CopyVariant, given?: string): Promise<void> {
    const record = this.all.get(key)
    if (record === undefined) return
    const url =
      given ??
      (variant === 'full'
        ? (record.fullUrl ?? (record.variant === 'full' ? record.url : null))
        : null)
    if (url === null) {
      this.fail(key, 'gone')
      return
    }
    const name = fileNameFor(record.episodeId, variant)
    // A delete of this file still waiting for the worker must not now take
    // the bytes this download is about to write.
    this.doomed.delete(name)
    const shared = this.sharedRecord(name, key)
    const bytes = shared === undefined ? 0 : await this.sizeOf(name)
    const current = this.all.get(key)
    if (current === undefined || current.userId !== this.owner || !awaitingServer(current)) return
    this.write({
      ...current,
      name,
      url,
      variant,
      state: 'queued',
      bytes,
      total: shared?.total ?? 0,
      etag: shared?.etag ?? null,
      fresh: shared === undefined,
      serverProgress: null,
      reason: null,
      message: null,
      updated_at: this.stamp(),
    })
    if (!this.queue.includes(key)) this.queue.push(key)
    this.pump()
  }

  /* --- Trips (M19 T6, FR-A12) ---------------------------------------------------- */

  /**
   * Keep one trip episode's small copy on this device: what the auto-keep
   * hook calls for an `available` episode. A record the device already has is
   * adopted ({@link adoptTrip}) rather than started again — and so a record
   * paused by hand stays paused. Rejects only when the episode's payload
   * cannot be had, as {@link start} does.
   */
  async keepTripCopy(input: TripCopyInput): Promise<void> {
    await this.hydrate()
    const owner = this.owner
    if (owner === null) return
    const key = keyOf(owner, input.episodeId)
    if (this.all.has(key)) {
      this.adoptTrip(input.episodeId, input.tripId, input.url)
      return
    }
    if (this.starting.has(key)) return
    this.starting.add(key)
    try {
      const info = await this.loadInfo(input.episodeId)
      if (this.owner !== owner || this.all.has(key)) return
      const at = this.stamp()
      this.write({
        userId: owner,
        episodeId: input.episodeId,
        animeId: info.anime.id,
        name: fileNameFor(input.episodeId, 'small'),
        url: null,
        variant: 'small',
        fullUrl: input.fullUrl ?? null,
        state: 'preparing',
        serverProgress: null,
        tripId: input.tripId,
        bytes: 0,
        total: 0,
        etag: null,
        fresh: true,
        reason: null,
        message: null,
        snapshot: { anime: info.anime, episode: info.episode, duration: info.duration },
        created_at: at,
        updated_at: at,
      })
      void this.persist()
      void this.keepCover(owner, info.anime.id, info.anime.cover_large_url ?? info.anime.cover_url)
    } finally {
      this.starting.delete(key)
    }
    await this.place(key, 'small', input.url)
  }

  /**
   * A record the device already has for one of a trip's episodes joins the
   * trip: a copy already on the device (either size) is confirmed at once; a
   * record still waiting on the server for a small copy, or one whose copy
   * vanished, takes the trip's copy (`url`, when it is available). Anything
   * else — queued, downloading, paused (by hand or not) — is left exactly as
   * it is, and confirms when it finishes.
   */
  adoptTrip(episodeId: number, tripId: number, url: string | null): void {
    const record = this.snapshot[episodeId]
    if (record === undefined) return
    const key = keyOfRecord(record)
    if (record.tripId !== tripId) {
      this.patch(key, {
        tripId,
        confirm: record.state === 'downloaded' ? 'pending' : undefined,
      })
    }
    const current = this.all.get(key)
    if (current === undefined) return
    const copyGone =
      current.state === 'failed' &&
      current.variant === 'small' &&
      (current.reason === 'gone' || current.reason === 'unprepared')
    if (url !== null && this.active !== key && (awaitingServer(current) || copyGone)) {
      this.asking.delete(key)
      this.queue = this.queue.filter((queued) => queued !== key)
      if (copyGone) {
        this.patch(key, { state: 'preparing', url: null, reason: null, message: null })
      }
      void this.place(key, 'small', url)
      return
    }
    this.confirm(key)
  }

  /** Every trip copy of the signed-in account the server has not heard about yet: tell it. */
  confirmPending(): void {
    for (const record of Object.values(this.snapshot)) this.confirm(keyOfRecord(record))
  }

  /** Whether the signed-in account took this trip episode off the device by hand. */
  isDeclined(tripId: number, episodeId: number): boolean {
    const owner = this.owner
    return owner !== null && this.declined.has(declineKey(owner, tripId, episodeId))
  }

  /** "Ask again": the auto-keep hook may fetch this trip episode once more. */
  forgetDecline(tripId: number, episodeId: number): void {
    const owner = this.owner
    if (owner === null) return
    const key = declineKey(owner, tripId, episodeId)
    if (!this.declined.delete(key)) return
    void this.notes.delete(key).catch(() => undefined)
    this.emit()
  }

  /**
   * `POST …/delivered` for one downloaded trip copy, unless it is confirmed
   * already or a confirmation is in flight. A failure leaves `confirm:
   * 'pending'` for the next try; a final no from the server ends the asking.
   */
  private confirm(key: string): void {
    const record = this.all.get(key)
    if (
      record === undefined ||
      record.userId !== this.owner ||
      record.tripId === undefined ||
      record.state !== 'downloaded' ||
      record.confirm === 'done' ||
      this.confirming.has(key)
    ) {
      return
    }
    const tripId = record.tripId
    this.confirming.add(key)
    if (record.confirm !== 'pending') this.patch(key, { confirm: 'pending' })
    const settled = (final: boolean) => {
      this.confirming.delete(key)
      const current = this.all.get(key)
      if (!final || current === undefined || current.tripId !== tripId) return
      if (current.confirm !== 'done') this.patch(key, { confirm: 'done' })
    }
    void this.confirmDelivered(tripId, record.episodeId, record.etag).then(
      () => {
        settled(true)
      },
      (error: unknown) => {
        settled(confirmIsFinal(error))
      },
    )
  }

  /* --- Watched copies leave by themselves (owner, 2026-10-08) ------------------------ */

  /** Whether this device removes watched copies ("Remove episodes once watched"). */
  getRemoveWatched = (): boolean => this.removeWatchedOn

  /** Turn the device's switch on or off; remembered in IndexedDB `player`. */
  setRemoveWatched(on: boolean): void {
    if (this.removeWatchedOn === on) return
    this.removeWatchedOn = on
    void this.notes.put(REMOVE_WATCHED_KEY, on).catch(() => undefined)
    this.emit()
  }

  /** Keep this episode whether it is watched or not (`true`), or let it go once watched. */
  setKeep(episodeId: number, keep: boolean): void {
    const record = this.snapshot[episodeId]
    if (record === undefined || (record.keep === true) === keep) return
    this.patch(keyOfRecord(record), { keep })
  }

  /**
   * The outbox's `announceWatched`: `true` when the server accepted this
   * account's completion of the episode, `false` for an un-mark. Marks this
   * account's record only (another account's copy of the episode is its
   * own); a record made after the announcement is not marked.
   */
  noteWatched(userId: number, episodeId: number, watched: boolean): Promise<void> {
    const run = this.watchedChain.then(async () => {
      await this.hydrate()
      const key = keyOf(userId, episodeId)
      const record = this.all.get(key)
      if (record === undefined || (record.watched === true) === watched) return
      this.patch(key, { watched })
    })
    this.watchedChain = run.catch(() => undefined)
    return this.watchedChain
  }

  /**
   * The player has this episode open: it is not removed for being watched
   * until every player holding it is left. Returns the function that leaves.
   */
  enterPlayer(episodeId: number): () => void {
    this.playing.set(episodeId, (this.playing.get(episodeId) ?? 0) + 1)
    let left = false
    return () => {
      if (left) return
      left = true
      const count = (this.playing.get(episodeId) ?? 1) - 1
      if (count <= 0) this.playing.delete(episodeId)
      else this.playing.set(episodeId, count)
    }
  }

  /** Whether {@link removeWatched} would take this record off the device at its next pass. */
  willRemove(record: DownloadRecord): boolean {
    return this.removeWatchedOn && record.watched === true && record.keep !== true
  }

  /**
   * Take every watched copy of the signed-in account off this device (see
   * the class comment for which). Never rejects.
   */
  async removeWatched(): Promise<void> {
    await this.hydrate()
    await this.passWatched()
  }

  private passWatched(): Promise<void> {
    this.removing ??= this.removeEachWatched().finally(() => {
      this.removing = null
    })
    return this.removing
  }

  private async removeEachWatched(): Promise<void> {
    const owner = this.owner
    if (owner === null) return
    for (const record of Object.values(this.snapshot)) {
      if (!this.willRemove(record) || this.playing.has(record.episodeId)) continue
      let queued: boolean
      try {
        queued = await this.queuedFor(owner, record.episodeId)
      } catch {
        // The outbox could not be read: keep the copy until it can.
        continue
      }
      if (queued) continue
      // Anything may have moved during the read.
      const current = this.snapshot[record.episodeId]
      if (
        this.owner !== owner ||
        current === undefined ||
        !this.willRemove(current) ||
        this.playing.has(record.episodeId)
      ) {
        continue
      }
      try {
        await this.remove(record.episodeId)
      } catch {
        // Tried again at the next pass.
      }
    }
  }

  /* --- The worker --------------------------------------------------------------- */

  /**
   * Another record that vouches for the bytes in `name` — the same copy of the
   * same episode kept by another account. A record with no URL yet vouches
   * for nothing.
   */
  private sharedRecord(name: string, except?: string): DownloadRecord | undefined {
    for (const [key, record] of this.all) {
      if (key === except || holdsNoFile(record)) continue
      if (record.name === name) return record
    }
    return undefined
  }

  private commandFor(record: DownloadRecord, url: string): WorkerCommand {
    return {
      cmd: 'download',
      name: record.name,
      url,
      etag: record.fresh === true ? null : record.etag,
      total: record.fresh === true || record.total <= 0 ? null : record.total,
      fresh: record.fresh === true,
      run: this.activeRun ?? undefined,
    }
  }

  /** Start the next queued download if nothing is running. */
  private pump(): void {
    if (this.active !== null) return
    if (this.spaceHeld()) {
      // Out of space: only the probe may run; everything else waits, queued.
      const probe = this.probe
      if (probe !== null && this.queue.includes(probe)) {
        this.queue = [probe, ...this.queue.filter((queued) => queued !== probe)]
      } else {
        this.wantWakeLock = false
        void this.releaseWakeLock()
        return
      }
    }
    while (this.queue.length > 0) {
      const key = this.queue.shift() ?? ''
      const record = this.all.get(key)
      if (
        record === undefined ||
        record.userId !== this.owner ||
        record.state !== 'queued' ||
        record.url === null
      ) {
        continue
      }
      this.active = key
      this.runs += 1
      this.activeRun = this.runs
      this.patch(key, { state: 'downloading', reason: null, message: null })
      this.wantWakeLock = true
      void this.holdWakeLock()
      // Never hand a download to a worker that just reported a write it
      // could not make: a new one gets it.
      if (this.retired && this.worker !== null) this.dropWorker()
      this.send(this.commandFor(record, record.url))
      return
    }
    this.wantWakeLock = false
    void this.releaseWakeLock()
  }

  /** Whether the signed-in account has a record paused for want of space: the queue is held. */
  private spaceHeld(): boolean {
    return Object.values(this.snapshot).some(
      (record) => record.state === 'paused' && record.reason === 'quota',
    )
  }

  /** A chunk of `key` landed: its strikes end, and a probe that worked lifts the space hold. */
  private wrote(key: string): void {
    this.strikes.delete(key)
    if (this.probe !== key) return
    this.probe = null
    for (const record of Object.values(this.snapshot)) {
      if (record.state !== 'paused' || record.reason !== 'quota') continue
      const other = keyOfRecord(record)
      if (!this.queue.includes(other)) this.queue.push(other)
      this.patch(other, { state: 'queued', reason: null, message: null })
    }
  }

  /**
   * The running record's worker stopped by itself (see the class comment):
   * out of space at once on a full disk, on a failed probe, or on the second
   * stop in a row; otherwise once more now on a new worker, or — off screen —
   * paused until Arc is back.
   */
  private suspend(key: string, reason: SuspendReason, detail: string | undefined): void {
    const strikes = (this.strikes.get(key) ?? 0) + 1
    this.strikes.set(key, strikes)
    const wasProbe = this.probe === key
    if (wasProbe) this.probe = null
    if (reason === 'quota' || wasProbe || strikes >= STRIKES_FOR_SPACE) {
      this.patch(key, {
        state: 'paused',
        reason: 'quota',
        message: MESSAGES.quota,
        autoResume: false,
      })
      return
    }
    if (this.isVisible()) {
      this.queue = [key, ...this.queue.filter((queued) => queued !== key)]
      this.patch(key, { state: 'queued', reason: null, message: null, autoResume: false })
      return
    }
    this.patch(key, {
      state: 'paused',
      reason: 'interrupted',
      message: detail === undefined ? BACKGROUNDED : `${BACKGROUNDED} (${detail})`,
      autoResume: true,
    })
  }

  /** Ask the worker to stop this file; it is *releasing* until the worker says it closed it. */
  private halt(record: DownloadRecord): void {
    this.releasing.add(record.name)
    this.send({ cmd: 'pause', name: record.name })
    if (this.active === keyOfRecord(record)) {
      this.active = null
      this.activeRun = null
    }
  }

  private send(command: WorkerCommand): void {
    this.worker ??= this.boot()
    this.worker.postMessage(command)
  }

  private boot(): WorkerLike {
    const worker = this.createWorker()
    worker.onMessage((message) => {
      this.onMessage(message)
    })
    worker.onError((reason) => {
      this.onWorkerError(worker, reason)
    })
    return worker
  }

  /**
   * The worker failed to load or died. Whatever it was writing fails with a
   * sentence, the worker is dropped so the next send boots a new one, and the
   * queue moves on — never a "downloading 0 %" that nothing will ever end.
   */
  private onWorkerError(worker: WorkerLike, reason: string): void {
    if (this.worker !== worker) return
    this.dropWorker()
    const key = this.active
    this.active = null
    this.activeRun = null
    if (key !== null) {
      this.patch(key, {
        state: 'failed',
        reason: 'error',
        message: `${WORKER_FAILED} (${reason})`,
      })
    }
    this.pump()
  }

  /**
   * Terminate the worker; the next send boots a new one. A terminated worker
   * holds no file, so whatever was releasing is released (and a delete
   * waiting on it runs).
   */
  private dropWorker(): void {
    const worker = this.worker
    this.worker = null
    this.retired = false
    try {
      worker?.terminate()
    } catch {
      // Already gone.
    }
    for (const name of this.releasing) this.settle(name)
    this.releasing.clear()
  }

  /** The record a worker message is about: the running one, else the owner's by file. */
  private keyFor(name: string): string | null {
    if (this.active !== null && this.all.get(this.active)?.name === name) return this.active
    let other: string | null = null
    for (const [key, record] of this.all) {
      if (record.name !== name) continue
      if (record.userId === this.owner) return key
      other ??= key
    }
    return other
  }

  /** The worker has closed `name`: run a delete that was waiting for it. */
  private settle(name: string): void {
    this.releasing.delete(name)
    if (!this.doomed.has(name)) return
    this.doomed.delete(name)
    void this.removeFrom(name)
  }

  /** Handle one worker message. Public for the tests; the worker is the only caller. */
  onMessage(message: WorkerMessage): void {
    if (isTerminal(message)) this.settle(message.name)
    // A queued command the worker dropped unopened: nothing more to learn.
    if (message.type === 'released') return
    if (
      (message.type === 'paused' && message.reason !== undefined) ||
      (message.type === 'failed' && (message.write === true || message.code === 'quota'))
    ) {
      // This worker could not write: it gets no further download.
      this.retired = true
    }
    const key = this.keyFor(message.name)
    if (key === null) return
    const record = this.all.get(key)
    if (record === undefined) return
    const current = message.run === undefined || message.run === this.activeRun
    if (!current) {
      // A run the manager already gave up on (paused, handed away, deleted):
      // it may only say how far it got, and only about a record not running.
      if (message.type === 'paused' && this.active !== key) {
        this.patch(key, { bytes: message.offset })
      }
      return
    }
    const running = this.active === key

    switch (message.type) {
      case 'progress':
        // A chunk that was in flight when he pressed pause still lands; it
        // moves the bytes but not the state.
        this.patch(key, {
          bytes: message.offset,
          total: message.total,
          etag: message.etag,
          fresh: false,
          ...(running ? { state: 'downloading' as const, reason: null, message: null } : {}),
        })
        if (running) this.wrote(key)
        break
      case 'restarted':
        this.patch(key, { bytes: 0, total: 0, etag: null, message: RESTARTED })
        break
      case 'retrying':
        if (running && message.attempt > QUIET_RETRIES) {
          this.patch(key, { state: 'paused', reason: 'network', message: MESSAGES.network })
        }
        break
      case 'busy':
        if (running) {
          this.patch(key, { state: 'paused', reason: 'elsewhere', message: MESSAGES.elsewhere })
          this.active = null
          this.activeRun = null
          this.pump()
        }
        break
      case 'paused':
        this.patch(key, { bytes: message.offset })
        if (running) {
          this.active = null
          this.activeRun = null
          // A pause nobody asked for: the device stopped the write.
          if (message.reason !== undefined) this.suspend(key, message.reason, message.detail)
          this.pump()
        }
        break
      case 'failed':
        if (!running && record.state !== 'downloading') {
          // The run it ended had already been paused or handed away.
          this.patch(key, { bytes: message.offset })
          break
        }
        if (message.code === 'quota') {
          // An older worker's word for a full disk: a pause, as now.
          this.patch(key, { bytes: message.offset })
          if (running) {
            this.active = null
            this.activeRun = null
          }
          this.suspend(key, 'quota', message.reason)
          this.pump()
          break
        }
        this.patch(key, {
          state: 'failed',
          bytes: message.offset,
          reason: message.code,
          message:
            message.code === 'error'
              ? `${MESSAGES.error} (${message.reason})`
              : MESSAGES[message.code],
        })
        if (running) {
          this.active = null
          this.activeRun = null
          this.pump()
        }
        // The server dropped the small copy (idle sweep, re-encode): ask it
        // again, once, and carry on from its answer.
        if (message.code === 'gone' && record.variant === 'small' && record.reasked !== true) {
          this.queue = this.queue.filter((queued) => queued !== key)
          this.patch(key, {
            state: 'preparing',
            url: null,
            serverProgress: null,
            reason: null,
            message: null,
            reasked: true,
          })
          void this.ask(key, 'request')
        }
        break
      case 'done':
        this.patch(key, {
          state: 'downloaded',
          bytes: message.offset,
          total: message.total,
          etag: message.etag,
          fresh: false,
          reason: null,
          message: null,
          reasked: false,
          // A trip copy that has arrived is told to the server (M19 T6).
          ...(record.tripId === undefined ? {} : { confirm: 'pending' as const }),
        })
        if (running) this.wrote(key)
        if (running) {
          this.active = null
          this.activeRun = null
          this.pump()
        }
        this.confirm(key)
        break
    }
  }

  /* --- Keeping the screen on ---------------------------------------------------- */

  private async holdWakeLock(): Promise<void> {
    if (this.wakeLock !== null || typeof navigator === 'undefined') return
    const lock = (
      navigator as Navigator & {
        wakeLock?: { request(type: 'screen'): Promise<WakeLockSentinelLike> }
      }
    ).wakeLock
    if (lock === undefined) return
    try {
      const sentinel = await lock.request('screen')
      this.wakeLock = sentinel
      // The browser releases it on its own when the page is hidden.
      sentinel.addEventListener?.('release', () => {
        if (this.wakeLock === sentinel) this.wakeLock = null
      })
    } catch {
      // Refused (not visible, battery saver). Downloads still work.
    }
  }

  private async releaseWakeLock(): Promise<void> {
    const held = this.wakeLock
    this.wakeLock = null
    try {
      await held?.release()
    } catch {
      // Already released by the browser.
    }
  }
}

let shared: DownloadManager | null = null

export function downloads(): DownloadManager {
  shared ??= new DownloadManager()
  return shared
}

/** Replace the shared manager. Tests only. */
export function setDownloads(replacement: DownloadManager | null): void {
  shared = replacement
}
