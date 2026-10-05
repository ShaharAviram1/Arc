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
 * | `queued` | waiting for the download ahead of it |
 * | `downloading` | a bar and `n %` |
 * | `paused` | why (`by-hand`, `interrupted`, `network`, `elsewhere`), and Resume |
 * | `downloaded` | the size, and a way to delete it |
 * | `failed` | why (`quota`, `auth`, `gone`, `size`, `error`, `evicted`, `unreadable`), and Try again |
 *
 * `paused` / `network` is a message, not a stop: the worker keeps retrying and
 * the next chunk that lands puts the record back to `downloading`.
 *
 * **One download at a time**, in the order they were asked for.
 *
 * **Records belong to a user.** Each is keyed by (user, episode) and only the
 * signed-in owner's are in the snapshot; another account on the same iPad sees
 * none of them and cannot play them. The file is named by episode, so two
 * accounts that keep the same episode share its bytes, and it is deleted only
 * when the last record naming it goes.
 *
 * **A file is never touched while the worker may hold it.** From the moment a
 * pause is sent until the worker's terminal message for that file (which the
 * worker posts only after closing it) the name is *releasing*; a delete in
 * that window is deferred to the terminal message. A new download of a file no
 * record vouches for starts `fresh` — the worker truncates it first — so an
 * orphan is never resumed into, and the launch sweep removes any orphan left.
 */

import { apiFetch } from '@/lib/api'
import type { PlayInfo } from '@/lib/playback'
import { forgetCover, rememberCover } from '@/offline/cache'
import {
  isTerminal,
  type FailCode,
  type WorkerCommand,
  type WorkerMessage,
} from '@/offline/download'
import {
  blobUrlFor,
  fileNameFor,
  fileSize,
  listEpisodeFiles,
  removeFile,
  requestPersistence,
  revokeCurrent,
} from '@/offline/opfs'
import { openStore, type KeyStore } from '@/offline/store'

export type DownloadState = 'queued' | 'downloading' | 'paused' | 'downloaded' | 'failed'

export type PauseReason = 'by-hand' | 'interrupted' | 'network' | 'elsewhere'

/** Failures the manager itself decides, beside the worker's. */
export type DeviceFailure = 'evicted' | 'unreadable'

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
  /** The OPFS file name. Derived from the episode id, never from input. */
  name: string
  /** The server's `download_url` for the episode. */
  url: string
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
  /** The `download_url` the show payload carried for the episode. */
  url: string
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
  quota: 'This device is out of space. Delete a download, then try again.',
  auth: 'Stopped: you are signed out, or this account may not download.',
  gone: 'Stopped: the server no longer has this episode.',
  size: 'The download did not finish cleanly. Try again to fetch the rest.',
  error: 'The download stopped. Try again.',
  evicted: 'Removed by the device to free space — Keep offline again.',
  unreadable: 'This download would not play. Try again to download it afresh.',
}

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

  private worker: WorkerLike | null = null
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
    const entries = await this.store.entries<DownloadRecord>()
    for (const [key, record] of entries) {
      if (typeof record !== 'object' || record === null) continue
      const bytes = await this.sizeOf(record.name)
      let next: DownloadRecord
      if (record.total > 0 && bytes === record.total) {
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
      } else if (record.state === 'failed') {
        next = { ...record, bytes }
      } else {
        const reason: PauseReason = record.reason === 'by-hand' ? 'by-hand' : 'interrupted'
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
  }

  /** Remove every episode file no record names: the orphans of an interrupted delete. */
  private async sweep(): Promise<void> {
    const named = new Set([...this.all.values()].map((record) => record.name))
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

  /** Start keeping an episode on this device. Needs the network (for its payload). */
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
      const name = fileNameFor(input.episodeId)
      // A delete of this file still waiting for the worker must not now take
      // the bytes this download is about to write.
      this.doomed.delete(name)
      const shared = this.sharedRecord(name)
      const at = this.stamp()
      this.write({
        userId: owner,
        episodeId: input.episodeId,
        animeId: info.anime.id,
        name,
        url: input.url,
        state: 'queued',
        // Another account's copy is vouched for and is resumed (or confirmed
        // whole); anything else on disk is an orphan and starts over.
        bytes: shared === undefined ? 0 : await this.sizeOf(name),
        total: shared?.total ?? 0,
        etag: shared?.etag ?? null,
        fresh: shared === undefined,
        reason: null,
        message: null,
        snapshot: { anime: info.anime, episode: info.episode, duration: info.duration },
        created_at: at,
        updated_at: at,
      })
      // Asked on the first download the viewer actually starts.
      void this.persist()
      void this.keepCover(owner, info.anime.id, info.anime.cover_large_url ?? info.anime.cover_url)
      this.queue.push(key)
      this.pump()
    } finally {
      this.starting.delete(key)
    }
  }

  pause(episodeId: number): void {
    const record = this.snapshot[episodeId]
    if (record === undefined || record.state === 'downloaded') return
    const key = keyOfRecord(record)
    if (this.active === key) this.halt(record)
    this.queue = this.queue.filter((queued) => queued !== key)
    this.patch(key, { state: 'paused', reason: 'by-hand', message: MESSAGES['by-hand'] })
    this.pump()
  }

  /** Resume a paused download, or try a failed one again. */
  resume(episodeId: number): void {
    const record = this.snapshot[episodeId]
    if (record === undefined || record.state === 'downloaded') return
    const key = keyOfRecord(record)
    if (this.active === key) {
      this.nudge()
      return
    }
    if (!this.queue.includes(key)) this.queue.push(key)
    this.patch(key, { state: 'queued', reason: null, message: null })
    this.pump()
  }

  /** Take an episode off this device. The server's copy is untouched. */
  async remove(episodeId: number): Promise<void> {
    const record = this.snapshot[episodeId]
    if (record === undefined) return
    const key = keyOfRecord(record)
    this.queue = this.queue.filter((queued) => queued !== key)
    this.urls.delete(key)
    this.all.delete(key)
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
    const key = this.active
    const active = key === null ? undefined : this.all.get(key)
    if (key !== null && active !== undefined) {
      if (active.state === 'paused' && active.reason === 'network') {
        this.patch(key, { state: 'downloading', reason: null, message: null })
      }
      this.send(this.commandFor(active))
      return
    }
    // A download another window was doing may be free by now.
    for (const record of Object.values(this.snapshot)) {
      if (record.state === 'paused' && record.reason === 'elsewhere') {
        this.resume(record.episodeId)
      }
    }
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
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onOnline)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('online', onOnline)
    }
  }

  /* --- The worker --------------------------------------------------------------- */

  private sharedRecord(name: string): DownloadRecord | undefined {
    for (const record of this.all.values()) if (record.name === name) return record
    return undefined
  }

  private commandFor(record: DownloadRecord): WorkerCommand {
    return {
      cmd: 'download',
      name: record.name,
      url: record.url,
      etag: record.fresh === true ? null : record.etag,
      total: record.fresh === true || record.total <= 0 ? null : record.total,
      fresh: record.fresh === true,
      run: this.activeRun ?? undefined,
    }
  }

  /** Start the next queued download if nothing is running. */
  private pump(): void {
    if (this.active !== null) return
    while (this.queue.length > 0) {
      const key = this.queue.shift() ?? ''
      const record = this.all.get(key)
      if (record === undefined || record.userId !== this.owner || record.state !== 'queued') {
        continue
      }
      this.active = key
      this.runs += 1
      this.activeRun = this.runs
      this.patch(key, { state: 'downloading', reason: null, message: null })
      this.wantWakeLock = true
      void this.holdWakeLock()
      this.send(this.commandFor(record))
      return
    }
    this.wantWakeLock = false
    void this.releaseWakeLock()
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
    this.worker = null
    try {
      worker.terminate()
    } catch {
      // Already gone.
    }
    // A dead worker holds no file.
    for (const name of this.releasing) this.settle(name)
    this.releasing.clear()
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
          this.pump()
        }
        break
      case 'failed':
        if (!running && record.state !== 'downloading') {
          // The run it ended had already been paused or handed away.
          this.patch(key, { bytes: message.offset })
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
        })
        if (running) {
          this.active = null
          this.activeRun = null
          this.pump()
        }
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
