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
 * `watched`. Completions the device never heard of live (made before this
 * build, or on another device) are caught up by {@link DownloadManager.syncWatched}:
 * at launch, at sign-in and on every nudge or auto-keep tick while online —
 * at most once per {@link WATCHED_SYNC_MS} per account — the server is asked
 * which of the account's unmarked records it holds a completion for, and a
 * record is marked when that completion is no older than the record (a copy
 * kept *after* an episode was finished is a rewatch, and stays).
 *
 * **The server hears what the device holds** (M20, FR-S9, architecture
 * §5.4d). {@link DownloadManager.reportHoldings} sends `PUT
 * /api/devices/{deviceId}/holdings` with every complete download of the
 * signed-in account not marked watched, so the acquisition window skips
 * those episodes. At launch, on reconnect, when a download completes and when
 * one is deleted; debounced 5 s; an unchanged set is not sent again within 10
 * minutes; with no network the report waits (`meta`) and goes on the outbox's
 * next landed flush or the reconnect; never for the demo account.
 *
 * **A file is never touched while the worker may hold it.** From the moment a
 * pause is sent until the worker's terminal message for that file (which the
 * worker posts only after closing it) the name is *releasing*; a delete in
 * that window is deferred to the terminal message. A new download of a file no
 * record vouches for starts `fresh` — the worker cuts it, but only once the
 * server has answered with the bytes that replace it — so an orphan is never
 * resumed into, and the launch sweep removes any orphan left.
 *
 * **A held copy is never destroyed** (M20.4 E, owner 2026-10-30, after a trip
 * copy confirmed at 03:30 was gone by 12:44). Nothing automatic drops a
 * downloaded file on doubtful evidence: the launch reads a short or missing
 * file twice before it says `evicted`, and a read the storage could not
 * answer changes nothing; Try again cuts nothing until the server has sent
 * bytes, and a run that ended without touching the file (`intact`) puts a
 * whole copy back to `downloaded`; a re-made trip copy downloads into the
 * file's other slot and the old one goes only when the new one is whole; a
 * media error re-checks the file and plays it once more before it is
 * `unreadable`. **Delivered means verified**: the worker hashes every byte it
 * writes (SHA-256), the confirmation and the holdings report carry the
 * digest and the size, and the one case a held file is dropped is the
 * server's own word that it is not its copy (`409 copy_mismatch`, or a
 * published digest that differs) — fetched again once, then `mismatch`. Every
 * such decision goes into an on-device log ({@link EventLog}) that "Send
 * report" posts to the server.
 */

import { apiFetch, ApiError, isUnreachable } from '@/lib/api'
import type { OfflineCopyOut } from '@/lib/anime'
import type { CompletedEpisode, PlayInfo } from '@/lib/playback'
import { forgetCover, rememberCover } from '@/offline/cache'
import {
  isTerminal,
  type FailCode,
  type SuspendReason,
  type WorkerCommand,
  type WorkerMessage,
} from '@/offline/download'
import { EventLog, type DeviceEvent } from '@/offline/eventLog'
import {
  blobUrlFor,
  bytesOf,
  fileNameFor,
  fileSize,
  listEpisodeFiles,
  deviceName,
  removeFile,
  requestPersistence,
  slotsFor,
  revokeCurrent,
  type CopyVariant,
  type SizeRead,
} from '@/offline/opfs'
import { offlineNow, subscribeNetwork } from '@/offline/network'
import { outbox } from '@/offline/outbox'
import { deviceId as storedDeviceId, isPersistent, openStore, type KeyStore } from '@/offline/store'

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

/**
 * Failures the manager itself decides, beside the worker's. `mismatch`
 * (M20.4 E): the copy's SHA-256 differed from the server's twice.
 */
export type DeviceFailure = 'evicted' | 'unreadable' | 'unprepared' | 'mismatch'

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
  /**
   * The server's ETag this trip copy was last replaced for ({@link
   * DownloadManager.adoptTrip}, owner 2026-10-28): a copy re-made on the
   * server is fetched again once per ETag, so a server that names one ETag
   * and serves another cannot make the device download for ever.
   */
  replacedFor?: string
  /**
   * The SHA-256 of the file on the device, hex, as the worker computed it
   * while the bytes arrived (M20.4 E); absent on records from before it, and
   * null when the worker could not read the file back for it.
   */
  sha256?: string | null
  /**
   * The SHA-256 the server published for this copy (`TripEpisodeOut.sha256`,
   * `OfflineCopyOut.sha256`), when it did: a finished download that differs
   * is fetched again once, then fails `mismatch`.
   */
  expected?: string | null
  /** One mismatch (the device's own check, or the server's 409) was already fetched again. */
  rechecked?: boolean
  /**
   * The copy this record held before a replacement started ({@link
   * DownloadManager.adoptTrip}, M20.4 E): it stays on the device, playable,
   * under its own file name until the new copy (in the other slot,
   * `altNameFor`) is whole; any other end of the replacement brings it back.
   */
  previous?: HeldCopy
  /**
   * A trip record left waiting (`preparing`, no URL) on a row the server
   * counts as delivered asked the server for it again by itself (M20.4 E).
   * Once per record until a download next finishes.
   */
  tripReasked?: boolean
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

/** A complete copy a replacement keeps until the new one is whole. */
export interface HeldCopy {
  name: string
  etag: string | null
  total: number
  sha256?: string | null
  confirm?: 'pending' | 'done'
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
  /** The copy's ETag (`TripEpisodeOut.etag`), so a re-made copy can replace the one held. */
  etag?: string | null
  /** The copy's SHA-256 (`TripEpisodeOut.sha256`), checked when the download finishes. */
  sha256?: string | null
}

/** What the trip payload says of one episode, beside its URL and ETag ({@link DownloadManager.adoptTrip}). */
export interface TripEpisodeFacts {
  /** The row's phase: `delivered` with a record still waiting is asked again (M20.4 E). */
  phase?: string
  sha256?: string | null
}

/** What the confirmation proves about the copy beside its ETag (M20.4 E). */
export interface DeliveryProof {
  sha256: string | null
  size: number
  /**
   * Which copy this is: the server checks the digest and size only against
   * its small copy (`small`), never for a full-size file (`full`).
   */
  variant: CopyVariant
}

/** One episode in the holdings report (`PUT /api/devices/{id}/holdings`, M20.4 E). */
export interface HeldEpisode {
  episode_id: number
  etag: string | null
  size: number
  sha256: string | null
  /** Which copy is held; the server checks digest and size for `small` only. */
  variant: CopyVariant
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
  mismatch: 'The copy on this device did not match the server’s. Try again to download it afresh.',
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
  deviceId: string,
  proof: DeliveryProof,
): Promise<unknown> {
  return apiFetch<null>(`/api/trips/${String(tripId)}/episodes/${String(episodeId)}/delivered`, {
    method: 'POST',
    body: JSON.stringify({
      etag,
      device_id: deviceId,
      sha256: proof.sha256,
      size: proof.size,
      variant: proof.variant,
    }),
  })
}

function defaultAskTripAgain(
  tripId: number,
  episodeId: number,
  deviceId: string,
): Promise<unknown> {
  return apiFetch<unknown>(`/api/trips/${String(tripId)}/episodes/${String(episodeId)}/again`, {
    method: 'POST',
    body: JSON.stringify({ device_id: deviceId }),
  })
}

function defaultPostReport(deviceId: string, events: DeviceEvent[]): Promise<unknown> {
  return apiFetch<null>(`/api/devices/${encodeURIComponent(deviceId)}/report`, {
    method: 'POST',
    body: JSON.stringify({ events }),
  })
}

function defaultReleaseDelivered(
  tripId: number,
  episodeId: number,
  deviceId: string,
): Promise<unknown> {
  const query = new URLSearchParams({ device_id: deviceId })
  return apiFetch<null>(
    `/api/trips/${String(tripId)}/episodes/${String(episodeId)}/delivered?${query.toString()}`,
    { method: 'DELETE' },
  )
}

/** The server's refusal of a trip call from a device that did not start the trip (2026-10-30). */
export const TRIP_OTHER_DEVICE = 'trip_other_device'

/**
 * The server's refusal of a confirmation whose ETag, SHA-256 or size is not
 * the copy it made (M20.4 E): the file on the device is wrong, and the one
 * case a held file is dropped — because the server said so.
 */
export const COPY_MISMATCH = 'copy_mismatch'

/** Whether a refusal is {@link COPY_MISMATCH}. */
function isCopyMismatch(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && refusalDetail(error) === COPY_MISMATCH
}

/**
 * A confirmation the server answered with a final no: the trip or the episode
 * is not this account's (404), this account may not keep copies (403), or the
 * trip belongs to another device (409 `trip_other_device`, owner 2026-10-30).
 * Nothing more can be told, so the record stops asking — and keeps its file:
 * a copy already on this device stays playable here.
 */
function confirmIsFinal(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  if (error.status === 404 || error.status === 403) return true
  return (
    error.status === 409 &&
    (refusalDetail(error) === TRIP_OTHER_DEVICE || refusalDetail(error) === COPY_MISMATCH)
  )
}

/** The `detail` of a refusal's body, when it is there. */
function refusalDetail(error: ApiError): unknown {
  const body = error.body
  return typeof body === 'object' && body !== null
    ? (body as { detail?: unknown }).detail
    : undefined
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

/** How often, at most, the server is asked which kept episodes were completed elsewhere. */
export const WATCHED_SYNC_MS = 10 * 60_000

/** The most episode ids one `GET /api/progress/completed` carries (the server's cap). */
export const COMPLETED_BATCH = 200

function defaultLoadCompleted(episodeIds: number[]): Promise<CompletedEpisode[]> {
  const query = episodeIds.map((id) => `episode_ids=${String(id)}`).join('&')
  return apiFetch<{ completed: CompletedEpisode[] }>(`/api/progress/completed?${query}`).then(
    (body) => body.completed,
  )
}

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
  const detail = refusalDetail(error)
  if (error.status === 409) return detail === 'source_gone' || detail === 'storage_held'
  if (error.status === 429) return detail === 'copy_queue_full'
  return false
}

/**
 * The file a record plays from: its own once downloaded, or the copy a
 * replacement keeps until the new one is whole (M20.4 E); `null` for none.
 */
function playableName(record: DownloadRecord): string | null {
  if (record.state === 'downloaded') return record.name
  return record.previous?.name ?? null
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

/** One stored record at launch: as read, and — when already decided — what it becomes. */
interface Launched {
  key: string
  record: DownloadRecord
  read: SizeRead
  final?: DownloadRecord
}

/** A downloaded copy whose first launch read says short or missing: read it again first. */
function doubts(record: DownloadRecord, read: SizeRead): boolean {
  return record.state === 'downloaded' && read !== 'unreadable' && bytesOf(read) !== record.total
}

/**
 * A record stored mid-replacement (Arc closed before the new copy was whole)
 * is its old copy again: the old file is still on the device under its own
 * name; the half-made new one is named by nothing, and the sweep takes it.
 */
function withoutReplacement(record: DownloadRecord): DownloadRecord {
  const previous = record.previous
  if (previous === undefined) return record
  return {
    ...record,
    name: previous.name,
    etag: previous.etag,
    total: previous.total,
    bytes: previous.total,
    sha256: previous.sha256 ?? null,
    confirm: previous.confirm,
    state: 'downloaded',
    reason: null,
    message: null,
    fresh: false,
    previous: undefined,
  }
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
  /**
   * What is already on disk: a size, `'missing'`, or `'unreadable'` (the
   * storage could not answer — never taken for missing). Injected so tests
   * need no OPFS.
   */
  sizeOf?: (name: string) => Promise<SizeRead>
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
  /**
   * `POST /api/trips/{id}/episodes/{eid}/delivered {etag, device_id, sha256,
   * size}` (M19 T6; device 2026-10-30; proof M20.4 E).
   */
  confirmDelivered?: (
    tripId: number,
    episodeId: number,
    etag: string | null,
    deviceId: string,
    proof: DeliveryProof,
  ) => Promise<unknown>
  /** `POST /api/trips/{id}/episodes/{eid}/again {device_id}`: a stuck trip record asks again (M20.4 E). */
  askTripAgain?: (tripId: number, episodeId: number, deviceId: string) => Promise<unknown>
  /** `POST /api/devices/{id}/report {events}`: the event log, sent by hand (M20.4 E). */
  postReport?: (deviceId: string, events: DeviceEvent[]) => Promise<unknown>
  /**
   * The size and SHA-256 of a file on the device, read off the main thread
   * (the worker's `hash` command). `size` null when it could not be read.
   */
  hashOf?: (name: string) => Promise<{ size: number | null; sha256: string | null }>
  /** The manager's event log (M20.4 E). Defaults to one over `meta`. */
  events?: EventLog
  /** How long the launch reconcile waits before reading a short or missing file again. */
  rereadMs?: number
  /**
   * Whether what the store writes survives a reload (`isPersistent`): a
   * replaced copy's old file is dropped only when it does.
   */
  durable?: () => Promise<boolean>
  /** `DELETE /api/trips/{id}/episodes/{eid}/delivered?device_id=`: the device deleted its copy. */
  releaseDelivered?: (tripId: number, episodeId: number, deviceId: string) => Promise<unknown>
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
  /** `GET /api/progress/completed`: which of these episodes the signed-in account completed, and when. */
  loadCompleted?: (episodeIds: number[]) => Promise<CompletedEpisode[]>
  /** Whether Arc can reach its server now (`offlineNow`, observed). */
  isOnline?: () => boolean
  /** `PUT /api/devices/{device_id}/holdings {episodes}` (M20, FR-S9; per-copy facts M20.4 E). */
  putHoldings?: (deviceId: string, report: HoldingsReport) => Promise<unknown>
  /** This device's own id ({@link deviceId}). */
  deviceId?: () => Promise<string>
  /** Where the last holdings report is remembered (IndexedDB `meta`). */
  meta?: KeyStore
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

/**
 * One holdings report's body: the per-copy shape (M20.4 E), or — while a
 * trip release still waits to reach the server — the older ids-only shape,
 * which the server never re-pends from (a missing id there is not read as
 * "lost", so a deletion whose release has not landed is not fetched again).
 */
export type HoldingsReport = { episodes: HeldEpisode[] } | { episode_ids: number[] }

function defaultPutHoldings(device: string, report: HoldingsReport): Promise<unknown> {
  return apiFetch<null>(`/api/devices/${encodeURIComponent(device)}/holdings`, {
    method: 'PUT',
    body: JSON.stringify(report),
  })
}

/** A trip release the server has not answered yet (`DELETE …/delivered`). */
export interface PendingRelease {
  trip_id: number
  episode_id: number
  /** When the copy was taken off the device (ISO 8601). */
  at: string
}

/** The `meta` key of an account's trip releases waiting to be sent. */
export function releasesKey(userId: number): string {
  return `trip-releases:${String(userId)}`
}

/**
 * Whether a release's failure is the server's final word: nothing to release
 * (404), the trip belongs elsewhere or has ended (409), this account may not
 * (403), or the request is not one it will ever take (422). A 401 (signed
 * out) stays queued for the next session.
 */
function releaseIsSettled(error: unknown): boolean {
  return error instanceof ApiError && [403, 404, 409, 422].includes(error.status)
}

/** How long the launch reconcile waits before it reads a short or missing file a second time. */
export const REREAD_MS = 1_500

/** How long a hash of a held file may take before it counts as unreadable. */
export const HASH_TIMEOUT_MS = 5 * 60_000

/** How long {@link DownloadManager.reportHoldings} waits for more changes before it sends. */
export const HOLDINGS_DEBOUNCE_MS = 5_000
/** An unchanged set is not sent again within this long (the server's TTL is 14 days). */
export const HOLDINGS_REFRESH_MS = 10 * 60_000
/** The server's cap on one report (`arc.services.acquisition.holdings.MAX_HOLDINGS`). */
export const MAX_HOLDINGS = 500
/** The `meta` key of an account's last holdings report. */
export function holdingsKey(userId: number): string {
  return `holdings:${String(userId)}`
}

/** What is remembered of the last report: the set, when it was sent, or that it waits. */
export interface HoldingsNote {
  ids: number[]
  /** The whole report (ids, ETags, sizes, digests) as sent, so a changed copy is sent again. */
  sig?: string
  /** When the server took this set (ms); 0 while it waits to be sent. */
  sentAt: number
  /** The set changed while Arc could not reach the server: send it on reconnect. */
  pending?: boolean
}

function sameIds(a: readonly number[], b: readonly number[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index])
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
  private readonly sizeOf: (name: string) => Promise<SizeRead>
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
    deviceId: string,
    proof: DeliveryProof,
  ) => Promise<unknown>
  private readonly askTripAgain: (
    tripId: number,
    episodeId: number,
    deviceId: string,
  ) => Promise<unknown>
  private readonly postReport: (deviceId: string, events: DeviceEvent[]) => Promise<unknown>
  private readonly hashOf: (name: string) => Promise<{ size: number | null; sha256: string | null }>
  private readonly rereadMs: number
  private readonly durable: () => Promise<boolean>
  /** Files the launch sweep leaves this time although no record names them (unread, M20.4 E). */
  private spared = new Set<string>()
  /** Old files of finished replacements, by episode, waiting for the player to be left. */
  private retiring = new Map<number, string[]>()
  /** The manager's diary (M20.4 E): every decision about a copy, for "Send report". */
  readonly events: EventLog
  private readonly releaseDelivered: (
    tripId: number,
    episodeId: number,
    deviceId: string,
  ) => Promise<unknown>
  private readonly notes: KeyStore
  private readonly queuedFor: (userId: number, episodeId: number) => Promise<boolean>
  private readonly loadCompleted: (episodeIds: number[]) => Promise<CompletedEpisode[]>
  private readonly isOnline: () => boolean
  private readonly putHoldings: (deviceId: string, report: HoldingsReport) => Promise<unknown>
  /** A flush of the queued trip releases in flight, so two triggers make one. */
  private releasing_: Promise<void> | null = null
  private readonly deviceId: () => Promise<string>
  private readonly meta: KeyStore

  /** The signed-in account is the demo one: it holds nothing and reports nothing. */
  private demo = false
  /** This device's own id once read ({@link thisDevice}). */
  private knownDevice: string | null = null
  /** The debounce of {@link reportHoldings}. */
  private holdingsTimer: ReturnType<typeof setTimeout> | null = null
  /** A report in flight, so a second waits for it rather than racing it. */
  private reporting: Promise<void> = Promise.resolve()

  /** "Remove episodes once watched", this device's switch. On until it is turned off. */
  private removeWatchedOn = true
  /** Episodes open in the player, with how many players hold each. Never removed for being watched. */
  private playing = new Map<number, number>()
  /** Watched marks applied in the order they were announced. */
  private watchedChain: Promise<void> = Promise.resolve()
  /** A pass of {@link removeWatched} in flight, so two triggers make one. */
  private removing: Promise<void> | null = null
  /** Announcements per `user:episode`, so a server answer read before one never overrides it. */
  private watchedNotes = new Map<string, number>()
  /** When each account last asked the server about completions ({@link syncWatched}). */
  private syncedAt = new Map<number, number>()
  /** A {@link syncWatched} in flight, so two triggers make one. */
  private syncing: Promise<void> | null = null

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
  /**
   * What a record was before Try again queued it (M20.4 E): a run that ends
   * without touching the file (`intact`) puts a whole copy back to
   * `downloaded` instead of leaving it failed. Cleared once a byte is written.
   */
  private prior = new Map<string, DownloadRecord>()
  /** Media errors per record since the player last opened it or its copy arrived ({@link playbackFailed}). */
  private playFailures = new Map<string, number>()
  /** Hash asks waiting for the worker's answer, by ask id. */
  private hashAsks = new Map<
    number,
    (answer: { size: number | null; sha256: string | null }) => void
  >()
  private hashCount = 0
  /** Trip records asking the server again right now (M20.4 E). */
  private tripAsking = new Set<string>()

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
    this.askTripAgain = options.askTripAgain ?? defaultAskTripAgain
    this.postReport = options.postReport ?? defaultPostReport
    this.hashOf = options.hashOf ?? ((name) => this.hashInWorker(name))
    this.rereadMs = options.rereadMs ?? REREAD_MS
    this.durable = options.durable ?? isPersistent
    this.releaseDelivered = options.releaseDelivered ?? defaultReleaseDelivered
    this.notes = options.notes ?? openStore('player')
    this.queuedFor = options.queuedFor ?? defaultQueuedFor
    this.loadCompleted = options.loadCompleted ?? defaultLoadCompleted
    this.isOnline = options.isOnline ?? (() => !offlineNow())
    this.putHoldings = options.putHoldings ?? defaultPutHoldings
    this.meta = options.meta ?? openStore('meta')
    this.deviceId = options.deviceId ?? (() => storedDeviceId(this.meta))
    this.events =
      options.events ??
      new EventLog(this.meta, this.now, import.meta.env.DEV && import.meta.env.MODE !== 'test')
  }

  /** Note one decision in the event log (M20.4 E). */
  /**
   * Note one decision in the event log (M20.4 E). `about` is the record it
   * concerns — its account tags the entry, so Send report never carries
   * another account's episodes — or an episode of the signed-in account, or
   * nothing for the device's own (the launch sweep, the worker).
   */
  private log(kind: string, about?: DownloadRecord | number, detail?: string): void {
    if (about === undefined) {
      this.events.note(kind, undefined, detail, null)
    } else if (typeof about === 'number') {
      this.events.note(kind, about, detail, this.owner)
    } else {
      this.events.note(kind, about.episodeId, detail, about.userId)
    }
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
    void this.store.put(keyOfRecord(record), record).catch(() => undefined)
  }

  private patch(key: string, patch: Partial<DownloadRecord>): DownloadRecord | null {
    const existing = this.all.get(key)
    if (existing === undefined) return null
    const next = { ...existing, ...patch, updated_at: this.stamp() }
    if (next.state !== existing.state) {
      // Every change of state, with its reason: the diary's backbone.
      const why = next.reason === null ? '' : ` (${next.reason})`
      this.log('state', next, `${existing.state} → ${next.state}${why}`)
    }
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
  setOwner(userId: number | null, options: { demo?: boolean } = {}): void {
    this.demo = userId !== null && options.demo === true
    if (this.owner === userId) return
    this.owner = userId
    if (this.holdingsTimer !== null) {
      // A report scheduled for the last account is not sent as this one's.
      clearTimeout(this.holdingsTimer)
      this.holdingsTimer = null
    }
    this.urls.clear()
    this.revokeUrl()
    const active = this.active === null ? undefined : this.all.get(this.active)
    if (active !== undefined && active.userId !== userId) this.halt(active)
    for (const [key, record] of this.all) {
      if (record.userId === userId) continue
      if (record.state === 'queued' || record.state === 'downloading') {
        this.events.note(
          'state',
          record.episodeId,
          `${record.state} → paused (interrupted): account changed`,
          record.userId,
        )
        this.all.set(key, {
          ...record,
          state: 'paused',
          reason: 'interrupted',
          message: MESSAGES.interrupted,
          autoResume: false,
          updated_at: this.stamp(),
        })
        void this.store.put(key, this.all.get(key)).catch(() => undefined)
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
      // And completions made elsewhere mark this account's copies.
      void this.syncWatched()
      // This account's launch, as far as the server's window is concerned.
      if (this.hydrated !== null) this.reportHoldings()
    }
    this.pump()
  }

  get ownerId(): number | null {
    return this.owner
  }

  /**
   * This device's own id (`offline/store.ts` `deviceId`): what a trip is
   * started with and what every confirmation and release names, so a trip
   * downloads only to the device that started it (owner, 2026-10-30).
   */
  thisDevice(): Promise<string> {
    if (this.knownDevice !== null) return Promise.resolve(this.knownDevice)
    return this.deviceId().then((device) => {
      this.knownDevice = device
      return device
    })
  }

  /**
   * Run a trip call that names this device: at once when its id is known
   * (read at launch), else once it has been read.
   */
  private withDevice(call: (device: string) => Promise<unknown>): Promise<unknown> {
    if (this.knownDevice !== null) return call(this.knownDevice)
    return this.thisDevice().then(call)
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
    const stored: Launched[] = []
    for (const [key, value] of await this.store.entries<DownloadRecord>()) {
      if (typeof value !== 'object' || value === null) continue
      const record = normalise(value)
      if (record.previous !== undefined) {
        stored.push({ key, ...(await this.settleReplacement(record)) })
        continue
      }
      // A record with no URL yet names no bytes; whatever is on disk under its
      // name is somebody else's, or an orphan.
      const read = holdsNoFile(record) ? 0 : await this.sizeOf(record.name)
      stored.push({ key, record, read })
    }
    // A downloaded copy that read short or missing is read once more, after a
    // pause, before anything is decided about it (M20.4 E). Until then it is
    // shown as it was stored, so the page is not empty meanwhile.
    const doubtful = stored.filter(
      (entry) => entry.final === undefined && doubts(entry.record, entry.read),
    )
    const shown = new Map<string, DownloadRecord>()
    for (const entry of stored) {
      const next = doubtful.includes(entry)
        ? entry.record
        : (entry.final ?? this.reconcileOne(entry.record, entry.read))
      // A launch-time record may have been written while this tab was
      // starting one of its own; the in-memory one is newer.
      if (!this.all.has(entry.key)) {
        this.all.set(entry.key, next)
        shown.set(entry.key, next)
      }
    }
    if (doubtful.length > 0) {
      this.emit()
      if (this.rereadMs > 0) await new Promise((resolve) => setTimeout(resolve, this.rereadMs))
      for (const entry of doubtful) {
        const { record, read: first } = entry
        entry.read = await this.sizeOf(record.name)
        this.log(
          'reconcile',
          record,
          `${record.name}: read ${String(first)}, then ${String(entry.read)}, of ${String(record.total)}`,
        )
        if (bytesOf(entry.read) === 0 && entry.read !== 'unreadable') {
          // Gone from its own slot: a whole copy in another slot no record
          // names is this one (a replacement whose record was not saved).
          const found = await this.wholeElsewhere(record)
          if (found !== null) {
            entry.record = { ...record, name: found }
            entry.read = record.total
            this.log('reconcile', record, `${record.name} gone; adopted ${found}, whole`)
          }
        }
        const next = this.reconcileOne(entry.record, entry.read)
        if (this.all.get(entry.key) === shown.get(entry.key)) this.all.set(entry.key, next)
      }
    }
    for (const entry of stored) {
      void this.store.put(entry.key, this.all.get(entry.key)).catch(() => undefined)
    }
    await this.sweep()
    this.emit()
    // A download that stopped because the session had lapsed carries on now
    // that somebody is signed in again (the same rule as `setOwner`).
    for (const record of Object.values(this.snapshot)) {
      if (record.state === 'failed' && record.reason === 'auth') this.resume(record.episodeId)
    }
    this.poll()
    // The trip calls name this device: its id is read once, here (not
    // awaited — a storage that never answers must not hold the launch).
    void this.thisDevice().catch(() => undefined)
    this.confirmPending()
    void this.flushReleases()
    await this.passWatched()
    void this.syncWatched()
    // Launch: tell the server what this device holds (M20, FR-S9).
    this.reportHoldings()
  }

  /**
   * One stored record against the disk, at launch (M20.4 E, owner
   * 2026-10-30), its file read (twice, when {@link doubts} — one odd read at
   * launch must not lose a copy). A size equal to the total is `downloaded`.
   * A downloaded record whose file is then definitely gone (or empty) is
   * `evicted`; one
   * that is short is `failed` / `size` (Try again fetches the rest). A read
   * the storage could not answer (`'unreadable'`) is evidence of nothing:
   * the record keeps its state and its bytes. Everything is logged.
   */
  /**
   * A record stored mid-replacement (Arc closed, or its last write did not
   * land, before the new copy was recorded whole). The old copy, when its
   * file is whole, is the record again — or, when the half-made new one has
   * bytes, stays the one that plays while the replacement resumes by itself.
   * A new copy that is whole while the old file is gone is adopted. Nothing
   * else is decided without reading both files (M20.4 E).
   */
  private async settleReplacement(
    record: DownloadRecord,
  ): Promise<{ record: DownloadRecord; read: SizeRead; final?: DownloadRecord }> {
    const previous = record.previous
    if (previous === undefined) return { record, read: await this.sizeOf(record.name) }
    const old = await this.sizeOf(previous.name)
    const fresh = await this.sizeOf(record.name)
    const oldWhole = previous.total > 0 && old === previous.total
    if (!oldWhole && record.total > 0 && fresh === record.total) {
      // An old file that could not be read stays on disk through this launch.
      if (old === 'unreadable') this.spared.add(previous.name)
      this.log(
        'reconcile',
        record,
        `${previous.name} ${String(old)}; adopted ${record.name}, whole`,
      )
      const adopted: DownloadRecord = {
        ...record,
        previous: undefined,
        state: 'downloaded',
        bytes: fresh,
        fresh: false,
        sha256: null,
        reason: null,
        message: null,
        ...(record.tripId === undefined ? {} : { confirm: 'pending' as const }),
      }
      return { record: adopted, read: fresh, final: adopted }
    }
    if (oldWhole && record.state !== 'failed' && typeof fresh === 'number' && fresh > 0) {
      this.log(
        'reconcile',
        record,
        `replacement ${record.name} at ${String(fresh)}; ${previous.name} plays meanwhile`,
      )
      const paused: DownloadRecord = {
        ...record,
        bytes: fresh,
        state: 'paused',
        reason: 'interrupted',
        message: BACKGROUNDED,
        autoResume: true,
      }
      return { record: paused, read: fresh, final: paused }
    }
    this.log('reconcile', record, `replacement ${record.name} undone; ${previous.name} again`)
    return { record: withoutReplacement(record), read: old }
  }

  /** Another record (any account) that names `name`, as its file or its kept copy. */
  private otherNaming(name: string, except: string): DownloadRecord | undefined {
    for (const [key, record] of this.all) {
      if (key === except) continue
      if (record.name === name || record.previous?.name === name) return record
    }
    return undefined
  }

  /** A slot of `record`'s copy, other than its own, that no record names and holds the whole copy. */
  private async wholeElsewhere(record: DownloadRecord): Promise<string | null> {
    if (record.total <= 0) return null
    const recorded = record.sha256 ?? null
    for (const slot of slotsFor(record.name)) {
      if (slot === record.name || this.namedByAny(slot)) continue
      if ((await this.sizeOf(slot)) !== record.total) continue
      if (recorded === null) return slot
      // A digest is known: the slot must be this very copy, read off the main thread.
      const found = await this.hashOf(slot)
      if (found.sha256 === recorded) return slot
      this.log(
        'reconcile',
        record,
        `${slot} is whole but sha256 ${String(found.sha256)}; not adopted`,
      )
    }
    return null
  }

  /** Whether any record (any account) names `name`, as its file or its kept copy. */
  private namedByAny(name: string): boolean {
    for (const record of this.all.values()) {
      if (record.name === name || record.previous?.name === name) return true
    }
    return false
  }

  private reconcileOne(record: DownloadRecord, read: SizeRead): DownloadRecord {
    if (record.state === 'preparing') {
      // The server carries on making it whether Arc is open or not; the
      // poll picks it up again.
      return { ...record, bytes: 0 }
    }
    if (read === 'unreadable') {
      // The storage did not answer: nothing is known about the file, so
      // nothing about the record changes but what a relaunch always changes.
      this.log('reconcile', record, `could not read ${record.name}; left ${record.state}`)
      if (record.state === 'downloaded' || record.state === 'failed') return record
      return this.relaunched(record, record.bytes)
    }
    const bytes = read === 'missing' ? 0 : read
    // A whole file is the copy again — unless nothing vouches for it (a fresh
    // record not evicted), or it was found unplayable or not the server's.
    const promotable =
      (record.fresh !== true || record.reason === 'evicted') &&
      !(
        record.state === 'failed' &&
        (record.reason === 'unreadable' || record.reason === 'mismatch')
      )
    if (record.total > 0 && bytes === record.total && promotable) {
      if (record.state !== 'downloaded') {
        this.log('reconcile', record, `${record.state} → downloaded (file whole)`)
      }
      return { ...record, bytes, state: 'downloaded', reason: null, message: null, fresh: false }
    }
    if (record.state === 'downloaded') {
      if (bytes === 0) {
        // Read twice, gone twice: the device took it back (eviction, or
        // storage cleared). Said, not hidden: the row offers Keep offline
        // again, and Delete.
        this.log('reconcile', record, `${record.name} ${String(read)} twice → evicted`)
        return {
          ...record,
          bytes,
          state: 'failed',
          reason: 'evicted',
          message: MESSAGES.evicted,
          fresh: true,
        }
      }
      // Short, but there: the same encode's beginning, which Try again finishes.
      this.log('reconcile', record, `${record.name} short twice (${String(bytes)}) → size`)
      return { ...record, bytes, state: 'failed', reason: 'size', message: MESSAGES.size }
    }
    if (record.state === 'failed' && record.reason === 'quota') {
      // Before 2026-10-07 a full device was a failure; it is a pause now.
      return { ...record, bytes, state: 'paused', message: MESSAGES.quota }
    }
    if (record.state === 'failed') return { ...record, bytes }
    return this.relaunched(record, bytes)
  }

  /** What a record that was moving when Arc closed comes back as: paused. */
  private relaunched(record: DownloadRecord, bytes: number): DownloadRecord {
    const reason: PauseReason =
      record.reason === 'by-hand' || record.reason === 'quota' ? record.reason : 'interrupted'
    if (record.state !== 'paused') {
      this.log('state', record, `${record.state} → paused (${reason}) at launch`)
    }
    return { ...record, bytes, state: 'paused', reason, message: MESSAGES[reason] }
  }

  /**
   * Remove every episode file no record names: the orphans of an interrupted
   * delete. A file named by any record is spared, whether or not the record
   * has a URL yet (M20.4 E), and so is every slot of a record mid-replacement.
   */
  private async sweep(): Promise<void> {
    const named = new Set<string>()
    for (const record of this.all.values()) {
      named.add(record.name)
      if (record.previous !== undefined) {
        named.add(record.previous.name)
        for (const slot of slotsFor(record.name)) named.add(slot)
      }
    }
    for (const name of await this.listFiles()) {
      if (!named.has(name) && !this.releasing.has(name) && !this.spared.has(name)) {
        this.log('dropped', undefined, `${name}: no record names it (launch sweep)`)
        await this.removeFrom(name)
      }
    }
  }

  /* --- Reading ---------------------------------------------------------------- */

  record(episodeId: number): DownloadRecord | undefined {
    return this.snapshot[episodeId]
  }

  /** Whether this episode plays from the device for the signed-in account. */
  isOnDevice(episodeId: number): boolean {
    const record = this.snapshot[episodeId]
    return record !== undefined && playableName(record) !== null
  }

  /**
   * A blob URL for a downloaded episode, or `null` to stream it instead.
   * Memoised, and the memo holds **one** episode: `blobUrlFor` revokes the
   * URL it minted last, so an older entry would name a dead URL.
   */
  async playableUrl(episodeId: number): Promise<string | null> {
    const record = this.snapshot[episodeId]
    const name = record === undefined ? null : playableName(record)
    if (record === undefined || name === null) return null
    const key = keyOfRecord(record)
    const known = this.urls.get(key)
    if (known !== undefined) return known
    const url = await this.blobUrl(name)
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
    if (record === undefined || playableName(record) === null) return null
    return this.urls.get(keyOfRecord(record)) ?? null
  }

  /**
   * The element would not play the file: the record becomes `failed` /
   * `unreadable`, so the row offers Try again (a fresh download) instead of
   * minting the same URL over the same bytes. Nothing on disk is touched —
   * that Try again cuts the file only once the server has sent its
   * replacement (M20.4 E). The player goes through {@link playbackFailed},
   * which calls this only for a second failure on a verified file.
   */
  markUnplayable(episodeId: number, detail = ''): void {
    const record = this.snapshot[episodeId]
    if (record === undefined) return
    const key = keyOfRecord(record)
    this.urls.delete(key)
    this.revokeUrl()
    this.log('unplayable', record, detail)
    this.patch(key, {
      state: 'failed',
      reason: 'unreadable',
      message: MESSAGES.unreadable,
      fresh: true,
    })
  }

  /**
   * The `<video>` refused this episode's file (M20.4 E, owner 2026-10-30).
   * A media error is not proof of a bad file — a blob URL revoked under a
   * playing element looks exactly the same — so the file is checked first:
   * gone (or empty) is `evicted`, short is `size`, a SHA-256 that differs
   * from the one recorded at download is `unreadable`. A file that checks
   * out is played once more from a freshly minted URL (the answer); only a
   * second failure on a verified file is `unreadable`. A file that could not
   * be read for the check is never marked: the player streams instead.
   * Resolves to the URL to try, or `null` to stream.
   */
  async playbackFailed(episodeId: number): Promise<string | null> {
    // The old URL stays live until a new one exists (minting revokes it) or
    // the answer is to stream.
    const url = await this.checkAfterError(episodeId)
    if (url === null) this.revokeUrl()
    return url
  }

  private async checkAfterError(episodeId: number): Promise<string | null> {
    const record = this.snapshot[episodeId]
    if (record === undefined || record.state !== 'downloaded') return null
    const key = keyOfRecord(record)
    this.urls.delete(key)
    const failures = (this.playFailures.get(key) ?? 0) + 1
    this.playFailures.set(key, failures)
    this.log('play-error', record, `media error ${String(failures)} on ${record.name}`)
    const read = await this.sizeOf(record.name)
    const still = (): boolean => this.all.get(key)?.state === 'downloaded'
    if (!still()) return null
    if (read === 'missing' || read === 0) {
      this.log('verify', record, `${record.name} ${String(read)}`)
      this.patch(key, {
        state: 'failed',
        bytes: 0,
        reason: 'evicted',
        message: MESSAGES.evicted,
        fresh: true,
      })
      return null
    }
    if (read === 'unreadable') {
      this.log('verify', record, `${record.name} could not be read; not marked`)
      return failures === 1 ? this.playableUrl(episodeId) : null
    }
    if (read !== record.total) {
      this.log('verify', record, `${record.name} is ${String(read)} of ${String(record.total)}`)
      this.patch(key, { state: 'failed', bytes: read, reason: 'size', message: MESSAGES.size })
      return null
    }
    let verified = true
    const recorded = record.sha256 ?? null
    if (recorded !== null) {
      const found = await this.hashOf(record.name)
      if (!still()) return null
      if (found.sha256 === null) {
        verified = false
        this.log('verify', record, `${record.name} could not be hashed`)
      } else if (found.sha256 !== recorded) {
        this.markUnplayable(episodeId, `sha256 ${found.sha256} is not ${recorded}`)
        return null
      }
    }
    if (failures === 1) {
      this.log('verify', record, `${record.name} checks out; playing again from a new URL`)
      return this.playableUrl(episodeId)
    }
    if (!verified) return null
    this.markUnplayable(episodeId, `a second media error on a verified file`)
    return null
  }

  /** The size and SHA-256 of a file on the device, from the worker; nulls when it cannot say. */
  private hashInWorker(name: string): Promise<{ size: number | null; sha256: string | null }> {
    this.hashCount += 1
    const ask = this.hashCount
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (this.hashAsks.delete(ask)) resolve({ size: null, sha256: null })
      }, HASH_TIMEOUT_MS)
      this.hashAsks.set(ask, (answer) => {
        clearTimeout(timer)
        resolve(answer)
      })
      this.send({ cmd: 'hash', name, ask })
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
    this.prior.delete(key)
    if (record.previous !== undefined) {
      // A replacement paused by hand: the copy on the device stays as it was.
      // `replacedFor` stays: only the server offering it again later retries.
      void this.keepPrevious(key, 'paused by hand')
      this.pump()
      return
    }
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
    // Try again on a copy that failed: should the run end without touching
    // the file, what it was comes back (M20.4 E).
    if (record.state === 'failed' || record.state === 'paused') this.prior.set(key, record)
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
    // Completions made elsewhere are caught up on the same beat (rate limited).
    void this.syncWatched()
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
      // it as held, and the auto-keep hook leaves it alone until "Ask again".
      // The release is queued durably and sent until the server answers
      // (M20.4 E): a lost one would let the next holdings report read the
      // deletion as a loss, and the episode would be fetched again.
      const declined = declineKey(record.userId, record.tripId, record.episodeId)
      this.declined.add(declined)
      void this.notes.put(declined, true).catch(() => undefined)
      await this.queueRelease(record.userId, record.tripId, record.episodeId)
      void this.flushReleases()
    }
    this.prior.delete(key)
    this.log('remove', record, `${record.state}${record.watched === true ? ', watched' : ''}`)
    await this.store.delete(key)
    this.emit()
    if (this.active === key) this.halt(record)
    // The worker may still hold the file; then it goes when the worker says
    // it has closed it.
    await this.dropFile(record.name)
    if (record.previous !== undefined) await this.dropFile(record.previous.name)
    const stillShown = [...this.all.values()].some(
      (other) => other.userId === record.userId && other.animeId === record.animeId,
    )
    if (!stillShown) await this.dropCover(record.userId, record.animeId)
    this.pump()
    // The device no longer holds it: the window may fetch it again (M20).
    this.reportHoldings()
  }

  /* --- What this device holds (M20, FR-S9, architecture §5.4d) ----------------- */

  /**
   * Tell the server, soon, which episodes this device holds for the
   * signed-in account, so its acquisition window skips them (FR-A15). Called
   * at launch, on reconnect, when a download completes and when one is
   * deleted; debounced by {@link HOLDINGS_DEBOUNCE_MS}, so a burst of
   * completions is one request. The set is every complete download not marked
   * watched, trip copies included (the trip path keeps its own `/delivered`
   * calls besides). Never for the demo account, which keeps no copies.
   */
  reportHoldings(): void {
    if (this.owner === null || this.demo) return
    if (this.holdingsTimer !== null) clearTimeout(this.holdingsTimer)
    this.holdingsTimer = setTimeout(() => {
      this.holdingsTimer = null
      void this.sendHoldings()
    }, HOLDINGS_DEBOUNCE_MS)
  }

  /**
   * A report the device could not deliver (no network) is sent now: what a
   * flush of the outbox that reached the server calls, the queue's own
   * "back online" moment.
   */
  async reportHoldingsIfPending(): Promise<void> {
    const owner = this.owner
    if (owner === null || this.demo) return
    // The outbox's flush landed: a release still waiting goes now too.
    void this.flushReleases()
    const note = await this.meta.get<HoldingsNote>(holdingsKey(owner))
    if (note?.pending === true) this.reportHoldings()
  }

  /**
   * The episodes this device holds for `userId` — complete (or kept through
   * a replacement), not watched — with each copy's ETag, size and SHA-256
   * (M20.4 E), sorted by id and capped.
   */
  private heldEpisodes(userId: number): HeldEpisode[] {
    const held = new Map<number, HeldEpisode>()
    for (const record of this.all.values()) {
      if (record.userId !== userId || record.watched === true) continue
      const copy: HeldCopy | undefined = record.state === 'downloaded' ? record : record.previous
      if (copy === undefined) continue
      held.set(record.episodeId, {
        episode_id: record.episodeId,
        etag: copy.etag,
        size: copy.total,
        sha256: copy.sha256 ?? null,
        variant: record.variant,
      })
    }
    // Over the server's cap, keep the highest ids: the newest episodes, which
    // are the ones a look-ahead window can still reach (an old episode's id is
    // behind every window), so the skip loses the least.
    return [...held.values()].sort((a, b) => a.episode_id - b.episode_id).slice(-MAX_HOLDINGS)
  }

  /** Send the set now (after the debounce), one report at a time. Never rejects. */
  sendHoldings(): Promise<void> {
    const run = this.reporting.then(() => this.sendHoldingsNow())
    this.reporting = run.catch(() => undefined)
    return this.reporting
  }

  private async sendHoldingsNow(): Promise<void> {
    const owner = this.owner
    if (owner === null || this.demo) return
    await this.hydrate()
    if (this.owner !== owner || this.demo) return
    const episodes = this.heldEpisodes(owner)
    const ids = episodes.map((episode) => episode.episode_id)
    // The per-copy shape only once no release of this account waits to be
    // sent; until then the ids-only shape, which the server never re-pends from.
    const legacy = (await this.pendingReleases(owner)).length > 0
    const report: HoldingsReport = legacy ? { episode_ids: ids } : { episodes }
    const sig = JSON.stringify(report)
    const key = holdingsKey(owner)
    const last = await this.meta.get<HoldingsNote>(key)
    if (last === undefined && ids.length === 0) return // Nothing held, nothing ever said.
    if (
      last !== undefined &&
      last.pending !== true &&
      sameIds(last.ids, ids) &&
      last.sig === sig &&
      this.now() - last.sentAt < HOLDINGS_REFRESH_MS
    ) {
      return
    }
    const waits: HoldingsNote = { ids: last?.ids ?? [], sentAt: last?.sentAt ?? 0, pending: true }
    if (!this.isOnline()) {
      // Kept for the reconnect: the outbox's next flush, or the browser's `online`.
      await this.meta.put(key, waits)
      return
    }
    try {
      await this.putHoldings(await this.deviceId(), report)
      await this.meta.put(key, { ids, sig, sentAt: this.now() } satisfies HoldingsNote)
      this.log(
        'holdings',
        undefined,
        `sent ${String(ids.length)}${legacy ? ' (ids only: a release waits)' : ''}: ${ids.join(',')}`,
      )
    } catch (error) {
      this.log('holdings', undefined, `not sent: ${String(error)}`)
      if (isUnreachable(error) || (error instanceof ApiError && error.status >= 500)) {
        // No answer, or the server failed: the next reconnect or landed flush
        // sends it again.
        await this.meta.put(key, waits)
      } else if (error instanceof ApiError && error.status === 403) {
        // The demo account: the server keeps nothing for it; neither do we.
        this.demo = true
      }
      // Anything else (a 4xx) is tried again at the next of the four moments.
    }
  }

  /* --- Trip releases, durably (M20.4 E) ----------------------------------------- */

  /** Remember a release to send, in `meta`, before anything is tried. */
  private async queueRelease(userId: number, tripId: number, episodeId: number): Promise<void> {
    const at = this.stamp()
    await this.meta
      .update<PendingRelease[]>(releasesKey(userId), (current) => {
        const list = Array.isArray(current) ? current : []
        if (list.some((item) => item.trip_id === tripId && item.episode_id === episodeId)) {
          return undefined
        }
        return [...list, { trip_id: tripId, episode_id: episodeId, at }]
      })
      .catch(() => undefined)
    this.events.note('release', episodeId, `trip ${String(tripId)}: queued`, userId)
  }

  /** The releases of `userId` still waiting for the server. */
  private async pendingReleases(userId: number): Promise<PendingRelease[]> {
    try {
      const found = await this.meta.get<PendingRelease[]>(releasesKey(userId))
      return Array.isArray(found) ? found : []
    } catch {
      return []
    }
  }

  /**
   * Send the signed-in account's queued releases, oldest first, until one
   * gets no answer. A 2xx, 404 or 409 settles a release (it is taken off the
   * queue); anything else leaves it for the next trigger: launch, return to
   * the screen, reconnect, a landed outbox flush, the next remove. Once the
   * queue is empty the holdings report goes again, in the per-copy shape.
   * Never rejects.
   */
  flushReleases(): Promise<void> {
    this.releasing_ ??= this.flushReleasesOnce().finally(() => {
      this.releasing_ = null
    })
    return this.releasing_
  }

  private async flushReleasesOnce(): Promise<void> {
    const owner = this.owner
    if (owner === null || this.demo) return
    let device: string | null = null
    let settledAny = false
    /** Releases tried in this flush: one queued meanwhile is tried too. */
    const tried = new Set<string>()
    for (;;) {
      const pending = (await this.pendingReleases(owner)).filter(
        (item) => !tried.has(`${String(item.trip_id)}:${String(item.episode_id)}`),
      )
      if (pending.length === 0 || this.owner !== owner) break
      try {
        device ??= await this.thisDevice()
      } catch {
        return
      }
      let unreachable = false
      for (const item of pending) {
        tried.add(`${String(item.trip_id)}:${String(item.episode_id)}`)
        let settled: boolean
        try {
          await this.releaseDelivered(item.trip_id, item.episode_id, device)
          settled = true
        } catch (error) {
          settled = releaseIsSettled(error)
          this.log(
            'release',
            item.episode_id,
            `trip ${String(item.trip_id)}: ${error instanceof ApiError ? String(error.status) : String(error)}${settled ? ' (settled)' : ''}`,
          )
          if (!settled && (isUnreachable(error) || !(error instanceof ApiError))) {
            unreachable = true
            break
          }
        }
        if (!settled) continue
        settledAny = true
        this.log('release', item.episode_id, `trip ${String(item.trip_id)}: done`)
        await this.meta
          .update<PendingRelease[]>(releasesKey(owner), (current) => {
            if (!Array.isArray(current)) return undefined
            const left = current.filter(
              (other) => !(other.trip_id === item.trip_id && other.episode_id === item.episode_id),
            )
            return left.length === 0 ? null : left
          })
          .catch(() => undefined)
      }
      if (unreachable) break
    }
    // No release waits any more: the per-copy report may go.
    if (settledAny && (await this.pendingReleases(owner)).length === 0) this.reportHoldings()
  }

  /**
   * "Send report" (M20.4 E): the whole event log to `POST
   * /api/devices/{id}/report`, for the operator to read with `arc.cli
   * device-report`. Resolves to how many entries went; rejects when the
   * server could not be told, so the page can say so. The log is kept.
   */
  async sendReport(): Promise<number> {
    // Only this account's entries, and the device's own (owner, 2026-10-30:
    // a shared iPad's report must not carry another account's episodes).
    const events = await this.events.entries(this.owner)
    const device = await this.thisDevice()
    await this.postReport(device, events)
    this.log('report', undefined, `sent ${String(events.length)} entries`)
    return events.length
  }

  /** The app is on screen again, or back online: carry on with whatever was running. */
  nudge(): void {
    // Whatever is waiting on the server asks now rather than at the next tick.
    this.poll()
    this.confirmPending()
    void this.flushReleases()
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
      // Reconnect: the server hears what this device holds (M20).
      this.reportHoldings()
    }
    // Arc's own observation of the network (a request answered again) is the
    // reconnect too, where the browser's `online` never fires.
    let wasOffline = offlineNow()
    const unsubscribe = subscribeNetwork(() => {
      const offline = offlineNow()
      if (wasOffline && !offline) {
        void this.flushReleases()
        this.reportHoldings()
      }
      wasOffline = offline
    })
    const hourly = setInterval(() => {
      void this.removeWatched()
    }, REMOVE_WATCHED_MS)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onOnline)
    return () => {
      unsubscribe()
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
        await (copy.url === null
          ? this.place(key, 'full')
          : this.place(key, 'small', copy.url, copy.sha256 ?? null))
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
  private async place(
    key: string,
    variant: CopyVariant,
    given?: string,
    expected: string | null = null,
  ): Promise<void> {
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
    const bytes = shared === undefined ? 0 : bytesOf(await this.sizeOf(name))
    const current = this.all.get(key)
    if (current === undefined || current.userId !== this.owner || !awaitingServer(current)) return
    this.log('state', current, `${current.state} → queued (${variant} copy)`)
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
      sha256: shared?.sha256 ?? null,
      expected,
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
      this.adoptTrip(input.episodeId, input.tripId, input.url, input.etag ?? null, {
        sha256: input.sha256 ?? null,
      })
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
    await this.place(key, 'small', input.url, input.sha256 ?? null)
  }

  /**
   * A record the device already has for one of a trip's episodes joins the
   * trip: a copy already on the device (either size) is confirmed at once; a
   * record still waiting on the server for a small copy, or one whose copy
   * vanished, takes the trip's copy (`url`, when it is available). Anything
   * else — queued, downloading, paused (by hand or not) — is left exactly as
   * it is, and confirms when it finishes.
   *
   * One exception to "confirmed at once" (owner, 2026-10-28): a small copy
   * on the device whose ETag differs from the one the server now offers
   * (`etag`, with a `url`) was re-made on the server with new encode settings
   * (`remake-copies`). Its file is dropped and the new copy downloaded in its
   * place through the usual queue, confirming with the new ETag when it
   * lands — not while the episode is open in the player (the next pass does
   * it), and once per server ETag ({@link DownloadRecord.replacedFor}). A
   * full-size copy, or either side's ETag unknown, is confirmed as before.
   */
  adoptTrip(
    episodeId: number,
    tripId: number,
    url: string | null,
    etag: string | null = null,
    facts: TripEpisodeFacts = {},
  ): void {
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
    // A replacement under way places the new copy itself.
    if (current === undefined || current.previous !== undefined) return
    const expected = facts.sha256 ?? null
    if (url !== null && etag !== null && this.remadeOnServer(current, etag)) {
      this.replaceCopy(key, url, etag, expected)
      return
    }
    if (facts.phase === 'delivered' && waitsForTrip(current) && current.state === 'preparing') {
      // The server counts this copy as delivered, but the device holds no
      // file for it (a 404 left it waiting with no URL): ask again, once.
      this.askAgainFor(key, tripId)
      return
    }
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
      void this.place(key, 'small', url, expected)
      return
    }
    this.confirm(key)
  }

  /**
   * `POST …/again` for a trip record stuck waiting (M20.4 E): once per
   * record ({@link DownloadRecord.tripReasked}). The server puts the row
   * back to pending, and the next trip payload offers the copy's URL, which
   * {@link adoptTrip} places as for any record waiting on its trip. No answer
   * at all is asked again on a later pass; any answer ends the asking.
   */
  private askAgainFor(key: string, tripId: number): void {
    const record = this.all.get(key)
    if (record === undefined || record.tripReasked === true || this.tripAsking.has(key)) return
    this.tripAsking.add(key)
    const { episodeId } = record
    this.log('reask', record, `trip ${String(tripId)}: delivered on the server, no file here`)
    void this.withDevice((device) => this.askTripAgain(tripId, episodeId, device))
      .then(
        () => {
          this.log('reask', record, 'asked again: the server re-pends it')
          this.patch(key, { tripReasked: true })
        },
        (error: unknown) => {
          this.log('reask', record, `refused: ${String(error)}`)
          if (!isUnreachable(error)) this.patch(key, { tripReasked: true })
        },
      )
      .finally(() => {
        this.tripAsking.delete(key)
      })
  }

  /** Whether `record` is a small copy the server has since re-made (see {@link adoptTrip}). */
  private remadeOnServer(record: DownloadRecord, etag: string): boolean {
    return (
      record.state === 'downloaded' &&
      record.variant === 'small' &&
      record.etag !== null &&
      record.etag !== etag &&
      record.replacedFor !== etag &&
      this.active !== keyOfRecord(record) &&
      !this.playing.has(record.episodeId)
    )
  }

  /**
   * Fetch the server's re-made small copy beside the one on the device
   * (M20.4 E, owner 2026-10-30): the new copy downloads into the file's
   * other slot (`altNameFor`) while the old one stays, playable, as the
   * record's `previous`. Only when the new copy is whole is the old file
   * dropped; any other end — a 404, a refusal, a pause, a worker that died,
   * Arc closing — brings the old copy back exactly as it was, but for
   * `replacedFor`, so the same server ETag is not chased again.
   */
  private replaceCopy(key: string, url: string, etag: string, expected: string | null): void {
    const record = this.all.get(key)
    if (record === undefined) return
    const previous: HeldCopy = {
      name: record.name,
      etag: record.etag,
      total: record.total,
      sha256: record.sha256 ?? null,
      confirm: record.confirm,
    }
    // A slot no record names — or one another account already holds this
    // very copy in, which is shared rather than cut (M20.4 E).
    let name: string | null = null
    let shared: DownloadRecord | undefined
    for (const slot of slotsFor(record.name)) {
      if (slot === record.name) continue
      const other = this.otherNaming(slot, key)
      if (other === undefined) {
        name = slot
        break
      }
      if (other.state === 'downloaded' && other.name === slot && other.etag === etag) {
        name = slot
        shared = other
        break
      }
    }
    if (name === null) {
      this.log('replace', record, `no free slot beside ${record.name}; kept as it is`)
      return
    }
    this.doomed.delete(name)
    this.urls.delete(key)
    this.log(
      'replace',
      record,
      `${record.name} (${String(record.etag)}) → ${name} (${etag}${shared === undefined ? '' : ', shared'}); the old copy stays until it is whole`,
    )
    this.patch(key, {
      previous,
      name,
      url,
      state: 'queued',
      etag: shared?.etag ?? null,
      bytes: shared?.total ?? 0,
      total: shared?.total ?? 0,
      sha256: shared?.sha256 ?? null,
      expected,
      fresh: shared === undefined,
      confirm: undefined,
      replacedFor: etag,
      reasked: false,
      serverProgress: null,
      reason: null,
      message: null,
    })
    if (!this.queue.includes(key)) this.queue.push(key)
    this.pump()
  }

  /**
   * A Try again (or Resume) that ended without touching the file (`intact`,
   * M20.4 E). A copy found unplayable or not the server's stays that, with
   * the server's words. Otherwise, whatever the record was — downloaded,
   * evicted, `size`, `error`, paused — a file still the size of its whole
   * copy is that copy: `downloaded` again, its URL and name kept, and its
   * digest checked afterwards when one is known (a mismatch marks it
   * `unreadable`, never drops it). Answers whether it handled the failure;
   * `false` leaves it to the usual path.
   */
  private restored(key: string, size: number, words: string): boolean {
    const prior = this.prior.get(key)
    this.prior.delete(key)
    if (prior === undefined) return false
    if (
      prior.state === 'failed' &&
      (prior.reason === 'unreadable' || prior.reason === 'mismatch')
    ) {
      this.log('kept', prior, `${prior.name} left as it was (${prior.reason})`)
      this.patch(key, {
        state: 'failed',
        bytes: size,
        total: prior.total,
        etag: prior.etag,
        fresh: true,
        reason: prior.reason,
        message: `${MESSAGES[prior.reason]} (${words})`,
      })
      return true
    }
    if (!(prior.total > 0 && size === prior.total)) return false
    this.log(
      'kept',
      prior,
      `${prior.name} is whole (${String(size)}) after ${prior.state}/${String(prior.reason)}: downloaded again`,
    )
    this.patch(key, {
      state: 'downloaded',
      bytes: size,
      total: prior.total,
      etag: prior.etag,
      sha256: prior.sha256 ?? null,
      fresh: false,
      reason: null,
      message: null,
    })
    const recorded = prior.sha256 ?? null
    if (recorded !== null) void this.checkDigest(key, recorded)
    return true
  }

  /** A restored copy's digest, checked off the main thread: a mismatch is `unreadable`, kept. */
  private async checkDigest(key: string, recorded: string): Promise<void> {
    const record = this.all.get(key)
    if (record === undefined) return
    const found = await this.hashOf(record.name)
    const current = this.all.get(key)
    if (current?.state !== 'downloaded' || current.name !== record.name) return
    if (found.sha256 !== null && found.sha256 !== recorded) {
      this.log('verify', record, `sha256 ${found.sha256} is not ${recorded}`)
      this.patch(key, {
        state: 'failed',
        reason: 'unreadable',
        message: MESSAGES.unreadable,
        fresh: true,
      })
    }
  }

  /**
   * Drop a replaced copy's old file — only once the record that names the
   * new one is durably stored (a lost write would leave a record pointing at
   * the old file), and never while the player has the episode open (the
   * drop then waits for it to be left). M20.4 E.
   */
  private async retireOld(key: string, record: DownloadRecord, oldName: string): Promise<void> {
    try {
      await this.store.put(key, record)
    } catch (error) {
      this.log('replace', record, `kept ${oldName}: the record was not saved (${String(error)})`)
      return
    }
    if (!(await this.durable())) {
      this.log('replace', record, `kept ${oldName}: storage is not persistent`)
      return
    }
    if (this.playing.has(record.episodeId)) {
      this.log('replace', record, `${oldName} goes when the player is left`)
      this.retiring.set(record.episodeId, [...(this.retiring.get(record.episodeId) ?? []), oldName])
      return
    }
    this.log('replace', record, `${record.name} is whole and saved; dropping ${oldName}`)
    await this.dropFile(oldName, key)
  }

  /**
   * A replacement that will not finish: the record is its old copy again
   * (all but `replacedFor`), and the half-made new file goes.
   */
  private async keepPrevious(
    key: string,
    why: string,
    { forget = false, dropNew = true }: { forget?: boolean; dropNew?: boolean } = {},
  ): Promise<void> {
    const record = this.all.get(key)
    const previous = record?.previous
    if (record === undefined || previous === undefined) return
    this.queue = this.queue.filter((queued) => queued !== key)
    this.log('replace', record, `kept ${previous.name}: ${why}`)
    this.patch(key, {
      name: previous.name,
      etag: previous.etag,
      total: previous.total,
      bytes: previous.total,
      sha256: previous.sha256 ?? null,
      confirm: previous.confirm,
      previous: undefined,
      state: 'downloaded',
      fresh: false,
      reason: null,
      message: null,
      // Not the server's doing: the same new copy may be tried again.
      ...(forget ? { replacedFor: undefined } : {}),
    })
    // The half-made new copy goes — unless another window is writing it.
    if (dropNew) await this.dropFile(record.name, key)
  }

  /**
   * Delete the file `name` unless another record (other than `except`)
   * still holds it; one the worker still has open goes when it lets go.
   */
  private async dropFile(name: string, except?: string): Promise<void> {
    // Another record vouches for these bytes (its own file, or a copy kept
    // through a replacement): kept.
    if (this.sharedRecord(name, except) !== undefined) return
    if (this.releasing.has(name)) {
      this.doomed.add(name)
      this.log('dropped', undefined, `${name}: once the worker lets go`)
      return
    }
    this.log('dropped', undefined, name)
    await this.removeFrom(name)
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
    const sent = record.etag
    this.confirming.add(key)
    if (record.confirm !== 'pending') this.patch(key, { confirm: 'pending' })
    const settled = (final: boolean) => {
      this.confirming.delete(key)
      const current = this.all.get(key)
      if (!final || current === undefined || current.tripId !== tripId) return
      if (current.etag !== sent) {
        // The copy was replaced while this was in flight: the answer was
        // about the old one, so the new one is confirmed now.
        this.confirm(key)
        return
      }
      if (current.confirm !== 'done') this.patch(key, { confirm: 'done' })
    }
    const { episodeId, etag } = record
    const proof: DeliveryProof = {
      sha256: record.sha256 ?? null,
      size: record.total,
      variant: record.variant,
    }
    void this.withDevice((device) =>
      this.confirmDelivered(tripId, episodeId, etag, device, proof),
    ).then(
      () => {
        this.log('confirmed', record, `trip ${String(tripId)} ${String(etag)}`)
        // The server took this copy: a mismatch before it is behind us.
        if (this.all.get(key)?.rechecked === true) this.patch(key, { rechecked: false })
        settled(true)
      },
      (error: unknown) => {
        if (error instanceof ApiError) {
          this.log(
            'server',
            record,
            `delivered → ${String(error.status)} ${String(refusalDetail(error))}`,
          )
        }
        const current = this.all.get(key)
        if (
          isCopyMismatch(error) &&
          current !== undefined &&
          current.variant === 'small' &&
          current.etag === sent
        ) {
          // The server says the file here is not its copy: the one case a
          // held file is dropped, and fetched again.
          this.confirming.delete(key)
          void this.fetchAgain(key, 'the server refused the copy (copy_mismatch)', {
            serverSaid: true,
          })
          return
        }
        settled(confirmIsFinal(error))
      },
    )
  }

  /**
   * The copy on the device is not the server's (its SHA-256 differed from the
   * published one, or the server answered `copy_mismatch`): the copy is
   * fetched afresh, once; a second mismatch is `failed` / `mismatch`. The
   * file is dropped only on the server's `copy_mismatch` (`serverSaid`);
   * otherwise the fresh run cuts it once the server has sent its
   * replacement. A replacement that mismatches twice keeps its old copy.
   */
  private async fetchAgain(
    key: string,
    why: string,
    { serverSaid }: { serverSaid: boolean },
  ): Promise<void> {
    const record = this.all.get(key)
    if (record === undefined) return
    this.log('mismatch', record, why)
    if (record.rechecked === true) {
      if (record.previous !== undefined) {
        await this.keepPrevious(key, `${why}, twice`)
        return
      }
      this.patch(key, {
        state: 'failed',
        reason: 'mismatch',
        message: MESSAGES.mismatch,
        fresh: true,
        bytes: serverSaid ? 0 : record.bytes,
        confirm: undefined,
      })
      // Dropped only on the server's own word; a digest the device found
      // different leaves the file (Try again cuts it once the server sends).
      if (serverSaid) await this.dropFile(record.name, key)
      return
    }
    this.patch(key, {
      state: 'queued',
      bytes: 0,
      total: 0,
      etag: null,
      sha256: null,
      fresh: true,
      rechecked: true,
      confirm: undefined,
      reason: null,
      message: null,
    })
    if (serverSaid) await this.dropFile(record.name, key)
    const current = this.all.get(key)
    if (current?.state !== 'queued') return
    if (!this.queue.includes(key)) this.queue.push(key)
    this.pump()
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
    const noted = keyOf(userId, episodeId)
    this.watchedNotes.set(noted, (this.watchedNotes.get(noted) ?? 0) + 1)
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
   * Catch up on completions the outbox never announced (owner, 2026-10-08):
   * ones made before this build, or on another device. While online, and at
   * most once per {@link WATCHED_SYNC_MS} per account, the signed-in
   * account's records not yet `watched` (nor kept) are sent to
   * `GET /api/progress/completed`, {@link COMPLETED_BATCH} ids a request —
   * one request for any number of shows. A record is marked `watched` when
   * the server holds a completion of it **no older than the record**: a copy
   * kept after the episode was finished is a rewatch, not a leftover. "Not
   * completed" never clears a mark, and an announcement that arrived while
   * the request was out wins over its answer. Anything marked goes through
   * the usual pass (the outbox, the player and `keep` still hold it). Never
   * rejects.
   */
  syncWatched(): Promise<void> {
    this.syncing ??= this.syncWatchedOnce().finally(() => {
      this.syncing = null
    })
    return this.syncing
  }

  private async syncWatchedOnce(): Promise<void> {
    await this.hydrate()
    const owner = this.owner
    if (owner === null || !this.isOnline()) return
    const last = this.syncedAt.get(owner)
    const at = this.now()
    if (last !== undefined && at - last < WATCHED_SYNC_MS) return
    const asked = Object.values(this.snapshot).filter(
      (record) => record.watched !== true && record.keep !== true,
    )
    this.syncedAt.set(owner, at)
    if (asked.length === 0) return
    const notesBefore = new Map(
      asked.map((record) => [keyOfRecord(record), this.watchedNotes.get(keyOfRecord(record)) ?? 0]),
    )
    const completed = new Map<number, number>()
    try {
      for (let start = 0; start < asked.length; start += COMPLETED_BATCH) {
        const ids = asked.slice(start, start + COMPLETED_BATCH).map((record) => record.episodeId)
        for (const item of await this.loadCompleted(ids)) {
          completed.set(item.episode_id, Date.parse(item.completed_at))
        }
      }
    } catch (error) {
      // No network after all: ask again at the next trigger rather than in ten minutes.
      if (isUnreachable(error)) this.syncedAt.delete(owner)
      return
    }
    let marked = false
    const apply = this.watchedChain.then(() => {
      if (this.owner !== owner) return
      for (const [episodeId, completedAt] of completed) {
        const key = keyOf(owner, episodeId)
        const record = this.all.get(key)
        if (record === undefined || record.watched === true || record.keep === true) continue
        if ((this.watchedNotes.get(key) ?? 0) !== notesBefore.get(key)) continue
        const kept = Date.parse(record.created_at)
        if (Number.isNaN(completedAt) || completedAt < kept) continue
        this.patch(key, { watched: true })
        marked = true
      }
    })
    this.watchedChain = apply.catch(() => undefined)
    await this.watchedChain
    if (marked) await this.passWatched()
  }

  /**
   * The player has this episode open: it is not removed for being watched
   * until every player holding it is left. Returns the function that leaves.
   */
  enterPlayer(episodeId: number): () => void {
    this.playing.set(episodeId, (this.playing.get(episodeId) ?? 0) + 1)
    // A new viewing: its first media error is played again ({@link playbackFailed}).
    const owner = this.owner
    if (owner !== null) this.playFailures.delete(keyOf(owner, episodeId))
    let left = false
    return () => {
      if (left) return
      left = true
      const count = (this.playing.get(episodeId) ?? 1) - 1
      if (count > 0) {
        this.playing.set(episodeId, count)
        return
      }
      this.playing.delete(episodeId)
      // A replacement finished while it played: its old file goes now.
      const waiting = this.retiring.get(episodeId) ?? []
      this.retiring.delete(episodeId)
      for (const name of waiting) {
        this.log('replace', episodeId, `player left; dropping ${name}`)
        void this.dropFile(name)
      }
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
      if (key === except) continue
      if (record.previous?.name === name) return record
      if (holdsNoFile(record)) continue
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
      if (this.all.get(key)?.previous !== undefined) {
        void this.keepPrevious(key, 'out of space')
        return
      }
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
    this.log('worker', undefined, `worker failed: ${reason}`)
    if (key !== null && this.all.get(key)?.previous !== undefined) {
      void this.keepPrevious(key, `the worker failed (${reason})`, { forget: true })
    } else if (key !== null) {
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
    // A hash the worker will now never answer: unreadable.
    for (const answer of this.hashAsks.values()) answer({ size: null, sha256: null })
    this.hashAsks.clear()
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
    if (message.type === 'hashed') {
      const answer = this.hashAsks.get(message.ask)
      this.hashAsks.delete(message.ask)
      answer?.({ size: message.size, sha256: message.sha256 })
      return
    }
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
      case 'truncated':
        // The server has sent the bytes that replace these: the old ones go now.
        this.prior.delete(key)
        this.log('truncated', record, `${message.name}: ${String(message.from)} bytes cut`)
        break
      case 'progress':
        this.prior.delete(key)
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
        // The file is not cut until the new bytes arrive, so until then it
        // is vouched for by nothing: the next run starts over too.
        this.log('restarted', record, message.reason)
        this.patch(key, { bytes: 0, total: 0, etag: null, fresh: true, message: RESTARTED })
        break
      case 'retrying':
        if (running && message.attempt > QUIET_RETRIES) {
          this.patch(key, { state: 'paused', reason: 'network', message: MESSAGES.network })
        }
        break
      case 'busy':
        if (running) {
          this.active = null
          this.activeRun = null
          if (record.previous !== undefined) {
            // That window is writing the new file: it is not ours to drop.
            void this.keepPrevious(key, 'another window holds the file', {
              forget: true,
              dropNew: false,
            })
          } else {
            this.patch(key, { state: 'paused', reason: 'elsewhere', message: MESSAGES.elsewhere })
          }
          this.pump()
        }
        break
      case 'paused':
        if (running && record.previous !== undefined && message.reason !== 'interrupted') {
          // Out of space (or a pause nobody else accounts for) during a
          // replacement: the old copy stays as it was, `replacedFor` too.
          this.active = null
          this.activeRun = null
          void this.keepPrevious(key, `stopped (${message.reason ?? 'paused'})`)
          this.pump()
          break
        }
        // An interrupted replacement keeps its half-made file and resumes
        // into it like any download, the old copy playing meanwhile.
        this.patch(key, { bytes: message.offset })
        if (running) {
          this.prior.delete(key)
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
        this.log(
          'failed',
          record,
          `${message.code}: ${message.reason}${message.intact === true ? ' (file untouched)' : ''}`,
        )
        if (record.previous !== undefined) {
          // A replacement that failed: the copy on the device stays.
          if (running) {
            this.active = null
            this.activeRun = null
          }
          void this.keepPrevious(key, `${message.code}: ${message.reason}`)
          this.pump()
          break
        }
        if (message.intact === true && this.restored(key, message.offset, message.reason)) {
          if (running) {
            this.active = null
            this.activeRun = null
            this.pump()
          }
          this.reportHoldings()
          break
        }
        this.prior.delete(key)
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
        // Never for a file the run left whole and untouched: a record with
        // no URL names no bytes, and those bytes are a copy (M20.4 E).
        if (
          message.code === 'gone' &&
          record.variant === 'small' &&
          record.reasked !== true &&
          !(message.intact === true && message.offset > 0)
        ) {
          this.log('reask', record, 'the small copy is gone (404): asking the server again')
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
      case 'done': {
        this.prior.delete(key)
        const sha256 = message.sha256 ?? null
        const expected = record.expected ?? null
        this.log(
          'done',
          record,
          `${message.name} ${String(message.offset)} bytes ${String(message.etag)} sha256 ${String(sha256)}`,
        )
        if (expected !== null && sha256 !== null && sha256 !== expected) {
          // Not the copy the server made: fetched again, once (M20.4 E).
          if (running) {
            this.wrote(key)
            this.active = null
            this.activeRun = null
          }
          void this.fetchAgain(key, `sha256 ${sha256} is not the server’s ${expected}`, {
            serverSaid: false,
          })
          if (running) this.pump()
          break
        }
        const previous = record.previous
        const done = this.patch(key, {
          state: 'downloaded',
          bytes: message.offset,
          total: message.total,
          etag: message.etag,
          sha256,
          fresh: false,
          reason: null,
          message: null,
          reasked: false,
          tripReasked: false,
          previous: undefined,
          // A digest the server published, and matched: no mismatch pending.
          ...(expected !== null && sha256 === expected ? { rechecked: false } : {}),
          // A trip copy that has arrived is told to the server (M19 T6).
          ...(record.tripId === undefined ? {} : { confirm: 'pending' as const }),
        })
        this.playFailures.delete(key)
        if (previous !== undefined && done !== null) {
          // The new copy is whole: the old one goes once that is on record.
          this.urls.delete(key)
          void this.retireOld(key, done, previous.name)
        }
        if (running) this.wrote(key)
        if (running) {
          this.active = null
          this.activeRun = null
          this.pump()
        }
        this.confirm(key)
        // A copy that has arrived is one the window can skip (M20).
        this.reportHoldings()
        break
      }
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
