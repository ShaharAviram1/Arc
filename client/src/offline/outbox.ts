/**
 * The on-device queue that keeps watch progress made offline (FR-S8, owner
 * 2026-10-04: "we gotta make sure we hold on to the records to sync them in
 * later when ipad is back online").
 *
 * The online path is untouched: the player still posts `/api/progress` every
 * ten seconds and beacons on `pagehide`. What lands here is what *could not*
 * be sent — a report whose request got no answer (or a 5xx / 401), the
 * mark-watched control pressed with no network, the exit report of a page
 * closing — and it is replayed through `POST /api/sync` on reconnect, on a
 * 15 s tick, when the page is hidden and on `pagehide`.
 *
 * The rules, each a thing that would otherwise go wrong.
 *
 * 1. **A completion is never lost to coalescing.** Position samples coalesce
 *    per (user, episode) — newest wins. The fact "this episode reached the
 *    completion mark" is its own record, created the moment a sample that
 *    *failed to send* crosses FR-S4's 90 % (or the user presses mark watched
 *    offline), under its own key, so a later lower sample cannot overwrite it.
 *    An un-mark is its own record too. A sample whose fate is merely unknown
 *    (the exit beacon) never mints a completion: it is queued as a position,
 *    which the server judges against anything newer, such as an un-mark.
 * 2. **Order is the device's own `seq`**, a per-device counter, never the
 *    device clock: a clock that steps backwards between a mark and an un-mark
 *    must not swap them. `at` (the clock) goes to the server only, for its
 *    comparison with what other devices did.
 * 3. **Nothing is dropped silently.** A record leaves on `applied` or `stale`,
 *    and only if it is still the record that was sent — checked atomically
 *    (`KeyStore.deleteIf`). `rejected` is kept, marked with the reason, and
 *    shown until dismissed. `retry`, a failed request, anything else: the
 *    record stays pending, with its attempts and last error counted; records
 *    stuck past {@link STUCK_AFTER_MS} and {@link STUCK_ATTEMPTS} are shown.
 * 4. **Records belong to a user.** A flush sends only the signed-in owner's;
 *    the server refuses a batch for anybody else (409). Signing in as someone
 *    else wipes nothing: their records wait and are counted as `foreign`.
 * 5. **No IndexedDB**: memory, and the snapshot's `persistent` says so.
 * 6. **One flush at a time** — in this tab (`inFlight`) and across tabs
 *    (`navigator.locks`, where there is one).
 *
 * Framework-free; the React side is `@/offline/useOutbox`.
 */

import { apiFetch } from '@/lib/api'
import { isPersistent, onPersistenceLost, openStore, type KeyStore } from '@/offline/store'

export const SYNC_PATH = '/api/sync'
/** The server's own cap (`arc.services.playback.sync.MAX_BATCH`). */
export const MAX_BATCH = 200
/** How often a running app flushes what it has queued. */
export const FLUSH_INTERVAL_MS = 15_000
/** FR-S4. Mirrors `arc.services.playback.progress.COMPLETION_FRACTION`. */
export const COMPLETION_FRACTION = 0.9
/** A record waiting at least this long after at least … */
export const STUCK_AFTER_MS = 10 * 60_000
/** … this many failed attempts is shown in the banner as "not yet synced". */
export const STUCK_ATTEMPTS = 3
/** The cross-tab lock name. */
export const LOCK_NAME = 'arc-outbox'

export type RecordKind = 'position' | 'completion' | 'unmark'

export interface OutboxRecord {
  /** Client-generated, echoed back by the server; changes when a sample coalesces. */
  id: string
  kind: RecordKind
  /** The account it was recorded under. Never sent as anyone else's. */
  user_id: number
  episode_id: number
  /** ISO-8601 from the device's clock: when it happened. For the server only. */
  at: string
  /** This device's order of events; replay and coalescing go by it. */
  seq: number
  position_s?: number
  duration_s?: number
  /** ISO time this record (or the first sample it coalesced from) was queued. */
  first_at?: string
  /** Failed sends so far, and the last reason. */
  attempts?: number
  last_error?: string
  /** The server's reason, once it has rejected this record. Kept until dismissed. */
  problem?: string
}

export type ItemStatus = 'applied' | 'stale' | 'rejected' | 'retry'

export interface SyncItemResult {
  client_id: string | null
  status: ItemStatus
  reason: string | null
}

export interface SyncResponse {
  results: SyncItemResult[]
}

export interface WireItem {
  client_id: string
  kind: RecordKind
  episode_id: number
  at: string
  position_s?: number
  duration_s?: number
}

export interface SyncBody {
  user_id: number
  /** The device's clock now; the server corrects every `at` by the skew. */
  sent_at: string
  items: WireItem[]
}

export type Send = (body: SyncBody, init: { keepalive: boolean }) => Promise<SyncResponse>

export interface FlushOutcome {
  /** False when a request never landed; everything stays queued. */
  ok: boolean
  sent: number
  removed: number
  rejected: number
}

export interface StuckSummary {
  count: number
  lastError: string | null
}

/** What the UI reads. Replaced only when something in it changed. */
export interface OutboxSnapshot {
  /** The owner's records the server rejected, in order. */
  problems: OutboxRecord[]
  /** The owner's records still waiting to be sent, in order. */
  pending: OutboxRecord[]
  /** `pending.length`. */
  waiting: number
  /** Pending records that have been failing for a while. */
  stuck: StuckSummary
  /** Records made under another account on this device, waiting for it. */
  foreign: number
  /** False when the queue lives in memory only and will not survive a reload. */
  persistent: boolean
}

const EMPTY: OutboxSnapshot = {
  problems: [],
  pending: [],
  waiting: 0,
  stuck: { count: 0, lastError: null },
  foreign: 0,
  persistent: true,
}

/** The slice of `navigator.locks` the flush uses. */
export interface Locks {
  request<T>(name: string, callback: () => Promise<T>): Promise<T>
}

const post: Send = (body, init) =>
  apiFetch<SyncResponse>(SYNC_PATH, {
    method: 'POST',
    body: JSON.stringify(body),
    keepalive: init.keepalive,
  })

function randomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${String(Date.now())}-${Math.random().toString(36).slice(2)}`
}

function browserLocks(): Locks | null {
  if (typeof navigator === 'undefined') return null
  const locks = (navigator as { locks?: LockManager }).locks
  if (locks === undefined) return null
  return {
    request: <T>(name: string, callback: () => Promise<T>) =>
      locks.request(name, callback) as Promise<T>,
  }
}

/**
 * The store key. Pending positions coalesce per (user, episode); everything
 * else is unique by id. A rejected record moves to a key of its own, so a new
 * sample for the same episode can never coalesce over a problem the user has
 * not seen yet.
 */
export function keyOf(
  record: Pick<OutboxRecord, 'kind' | 'user_id' | 'episode_id' | 'id' | 'problem'>,
): string {
  if (record.problem !== undefined) return `rejected:${record.id}`
  return record.kind === 'position'
    ? `position:${String(record.user_id)}:${String(record.episode_id)}`
    : `${record.kind}:${record.id}`
}

/** This device's order of events. */
export function bySeq(a: OutboxRecord, b: OutboxRecord): number {
  return a.seq - b.seq
}

export function crossesCompletion(position: number, duration: number): boolean {
  return duration > 0 && position / duration >= COMPLETION_FRACTION
}

function toWire(record: OutboxRecord): WireItem {
  const item: WireItem = {
    client_id: record.id,
    kind: record.kind,
    episode_id: record.episode_id,
    at: record.at,
  }
  if (record.position_s !== undefined) item.position_s = record.position_s
  if (record.duration_s !== undefined) item.duration_s = record.duration_s
  return item
}

function errorText(error: unknown): string {
  if (error instanceof Error && error.message !== '') return error.message
  return 'no connection'
}

/** One more failed attempt on `record`, if it is still the stored one. */
function failed(record: OutboxRecord, reason: string) {
  return (current: OutboxRecord | undefined): OutboxRecord | undefined =>
    current?.id === record.id
      ? { ...current, attempts: (current.attempts ?? 0) + 1, last_error: reason }
      : undefined
}

/** `announceWatched`'s listener: `watched` is true only for a completion the server accepted. */
export type WatchedListener = (userId: number, episodeId: number, watched: boolean) => void

export interface OutboxOptions {
  store?: KeyStore
  send?: Send
  now?: () => Date
  newId?: () => string
  persistent?: () => Promise<boolean>
  /** Cross-tab exclusion; `null` for none. Defaults to `navigator.locks`. */
  locks?: Locks | null
}

export interface RecordOptions {
  /**
   * Whether a sample past the mark may mint a completion record. False for a
   * sample whose fate is unknown (rule 1) and for one the server has already
   * said is completed.
   */
  completion?: boolean
}

export class Outbox {
  private readonly store: KeyStore
  private readonly send: Send
  private readonly now: () => Date
  private readonly newId: () => string
  private readonly checkPersistent: () => Promise<boolean>
  private readonly locks: Locks | null

  private owner: number | null = null
  /** The last account anybody signed in as; what new records are attributed to. */
  private recorder: number | null = null
  private seq: Promise<number> | null = null
  private inFlight: Promise<FlushOutcome> | null = null
  private snapshot: OutboxSnapshot = EMPTY
  private snapshotKey = JSON.stringify(EMPTY)
  private readonly listeners = new Set<() => void>()
  private readonly flushListeners = new Set<(outcome: FlushOutcome) => void>()
  /** `user:episode` the server has answered `completed: true` for, this page. */
  private readonly completed = new Set<string>()
  private readonly watchedListeners = new Set<WatchedListener>()
  private readonly appliedListeners = new Set<(record: OutboxRecord) => void>()

  constructor(options: OutboxOptions = {}) {
    this.store = options.store ?? openStore('outbox')
    this.send = options.send ?? post
    this.now = options.now ?? (() => new Date())
    this.newId = options.newId ?? randomId
    this.checkPersistent = options.persistent ?? isPersistent
    this.locks = options.locks === undefined ? browserLocks() : options.locks
  }

  /**
   * Who is signed in, or null. Records are attributed to the latest non-null
   * owner even after sign-out, and only the current owner's are ever sent.
   */
  setOwner(userId: number | null): void {
    this.owner = userId
    if (userId !== null) this.recorder = userId
    void this.refresh()
  }

  /** The account new records are made under, or null before anybody signed in. */
  get recordingAs(): number | null {
    return this.recorder
  }

  /** What the server last said about this episode's completion, on this page. */
  noteCompleted(userId: number, episodeId: number, completed: boolean): void {
    const key = `${String(userId)}:${String(episodeId)}`
    if (completed) this.completed.add(key)
    else this.completed.delete(key)
  }

  /**
   * Tell whoever listens ({@link onWatched}) that this account's watched state
   * for the episode moved: `true` **only** when the server has accepted a
   * completion (an online report at or past the mark answered `completed`,
   * the mark-watched call answered, or a replayed completion came back
   * `applied`), `false` for any un-mark — the server's or one still queued
   * here. Never `true` for a completion that is still in the queue. This is
   * the one signal the download manager removes a watched copy on (FR-S9,
   * owner 2026-10-08).
   */
  announceWatched(userId: number, episodeId: number, watched: boolean): void {
    for (const listener of this.watchedListeners) listener(userId, episodeId, watched)
  }

  /** Called on every {@link announceWatched}. Returns an unsubscribe. */
  onWatched(listener: WatchedListener): () => void {
    this.watchedListeners.add(listener)
    return () => {
      this.watchedListeners.delete(listener)
    }
  }

  /**
   * Called with every record the server answered `applied` — the moment the
   * server holds what it carries. The player's resume rule notes a position
   * here (FR-S2, 2026-10-08). Returns an unsubscribe.
   */
  onApplied(listener: (record: OutboxRecord) => void): () => void {
    this.appliedListeners.add(listener)
    return () => {
      this.appliedListeners.delete(listener)
    }
  }

  knownCompleted(userId: number, episodeId: number): boolean {
    return this.completed.has(`${String(userId)}:${String(episodeId)}`)
  }

  /**
   * A position sample that could not be sent. Coalesces with the queued one
   * for the episode (newest `seq` wins, atomically). If it is past the
   * completion mark, `completion` allows it, and no completion is queued
   * since the last un-mark, a completion record is added too.
   */
  async recordPosition(
    userId: number,
    episodeId: number,
    position: number,
    duration: number,
    { completion = true }: RecordOptions = {},
  ): Promise<void> {
    if (!Number.isFinite(position) || !Number.isFinite(duration) || duration <= 0) return
    const at = this.now().toISOString()
    const record: OutboxRecord = {
      id: this.newId(),
      kind: 'position',
      user_id: userId,
      episode_id: episodeId,
      at,
      seq: await this.nextSeq(),
      position_s: Math.max(0, position),
      duration_s: duration,
      first_at: at,
      attempts: 0,
    }
    await this.store.update<OutboxRecord>(keyOf(record), (current) => {
      if (current !== undefined && current.seq > record.seq) return undefined
      // Still the same waiting item: it has been waiting since the first sample.
      return current === undefined
        ? record
        : {
            ...record,
            first_at: current.first_at ?? current.at,
            attempts: current.attempts ?? 0,
            ...(current.last_error !== undefined ? { last_error: current.last_error } : {}),
          }
    })
    if (
      completion &&
      crossesCompletion(position, duration) &&
      !this.knownCompleted(userId, episodeId) &&
      !(await this.completionQueued(userId, episodeId))
    ) {
      await this.add('completion', userId, episodeId, at, position, duration)
    }
    await this.refresh()
  }

  /** The mark-watched control, pressed with no connection (FR-W3). */
  async recordCompletion(userId: number, episodeId: number): Promise<void> {
    await this.add('completion', userId, episodeId, this.now().toISOString())
    await this.refresh()
  }

  /** The explicit un-mark, pressed with no connection (FR-S4). */
  async recordUnmark(userId: number, episodeId: number): Promise<void> {
    this.noteCompleted(userId, episodeId, false)
    this.announceWatched(userId, episodeId, false)
    await this.add('unmark', userId, episodeId, this.now().toISOString())
    await this.refresh()
  }

  /** Every record, in this device's order. */
  async all(): Promise<OutboxRecord[]> {
    const entries = await this.store.entries<OutboxRecord>()
    return entries
      .map(([, record]) => record)
      .filter((record): record is OutboxRecord => typeof record === 'object' && record !== null)
      .sort(bySeq)
  }

  /** Delete one record the user has chosen to give up on. The only other way out. */
  async dismiss(id: string): Promise<void> {
    const record = (await this.all()).find((candidate) => candidate.id === id)
    if (record !== undefined) await this.store.deleteIf(keyOf(record), id)
    await this.refresh()
  }

  /**
   * Send the owner's pending records, in order, in batches of
   * {@link MAX_BATCH}. `keepalive` is for `pagehide`: the request outlives the
   * page, which is the only way the last records survive a closed tab.
   */
  flush({ keepalive = false }: { keepalive?: boolean } = {}): Promise<FlushOutcome> {
    if (this.inFlight !== null) return this.inFlight
    const locked =
      this.locks === null
        ? this.run(keepalive)
        : this.locks.request(LOCK_NAME, () => this.run(keepalive))
    const run = locked.finally(() => {
      this.inFlight = null
    })
    this.inFlight = run
    return run
  }

  /** Called after every flush that landed. Returns an unsubscribe. */
  onFlushed(listener: (outcome: FlushOutcome) => void): () => void {
    this.flushListeners.add(listener)
    return () => {
      this.flushListeners.delete(listener)
    }
  }

  getSnapshot = (): OutboxSnapshot => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** Re-read the store into the snapshot the UI renders; notify only on a change. */
  async refresh(): Promise<void> {
    const records = await this.all()
    const owner = this.owner
    const mine = records.filter((record) => owner !== null && record.user_id === owner)
    const pending = mine.filter((record) => record.problem === undefined)
    const now = this.now().getTime()
    const stuck = pending.filter(
      (record) =>
        (record.attempts ?? 0) >= STUCK_ATTEMPTS &&
        now - Date.parse(record.first_at ?? record.at) >= STUCK_AFTER_MS,
    )
    const next: OutboxSnapshot = {
      problems: mine.filter((record) => record.problem !== undefined),
      pending,
      waiting: pending.length,
      stuck: {
        count: stuck.length,
        lastError: stuck.reduce<string | null>((last, record) => record.last_error ?? last, null),
      },
      foreign: records.filter((record) => record.user_id !== owner).length,
      persistent: await this.checkPersistent(),
    }
    const key = JSON.stringify(next)
    if (key === this.snapshotKey) return
    this.snapshot = next
    this.snapshotKey = key
    for (const listener of this.listeners) listener()
  }

  private async run(keepalive: boolean): Promise<FlushOutcome> {
    const outcome: FlushOutcome = { ok: true, sent: 0, removed: 0, rejected: 0 }
    const owner = this.owner
    if (owner === null) return outcome

    for (;;) {
      const pending = (await this.all()).filter(
        (record) => record.user_id === owner && record.problem === undefined,
      )
      if (pending.length === 0) break
      const batch = pending.slice(0, MAX_BATCH)

      let answer: SyncResponse
      try {
        answer = await this.send(
          { user_id: owner, sent_at: this.now().toISOString(), items: batch.map(toWire) },
          { keepalive },
        )
      } catch (error) {
        // Offline, a 5xx, a 409, a captive portal: nothing is removed, and
        // every record in the batch counts one more failed attempt.
        outcome.ok = false
        const reason = errorText(error)
        for (const record of batch) {
          await this.store.update<OutboxRecord>(keyOf(record), failed(record, reason))
        }
        break
      }
      outcome.sent += batch.length

      const settled = await this.settle(batch, answer.results)
      outcome.removed += settled.removed
      outcome.rejected += settled.rejected
      // A short batch was the last; a batch the server took nothing from
      // (all `retry`, or unanswered) would otherwise loop for ever.
      if (batch.length < MAX_BATCH || settled.removed + settled.rejected === 0) break
    }

    await this.refresh()
    if (outcome.ok) for (const listener of this.flushListeners) listener(outcome)
    return outcome
  }

  private async settle(
    batch: OutboxRecord[],
    results: SyncItemResult[],
  ): Promise<{ removed: number; rejected: number }> {
    const byId = new Map<string, SyncItemResult>()
    for (const result of results) {
      if (result.client_id !== null) byId.set(result.client_id, result)
    }
    let removed = 0
    let rejected = 0
    for (const record of batch) {
      const result = byId.get(record.id)
      if (result === undefined) continue
      const key = keyOf(record)
      if (result.status === 'applied' || result.status === 'stale') {
        // Atomic: a newer sample coalesced in since the send has another id
        // and is not deleted.
        if (await this.store.deleteIf(key, record.id)) removed += 1
        if (result.status === 'applied') {
          this.announceApplied(record)
          for (const listener of this.appliedListeners) listener(record)
        }
      } else if (result.status === 'rejected') {
        // Kept under a key of its own first, then taken off the pending key —
        // so a crash in between leaves it twice, never nowhere.
        const kept: OutboxRecord = { ...record, problem: result.reason ?? 'rejected' }
        await this.store.put(keyOf(kept), kept)
        await this.store.deleteIf(key, record.id)
        rejected += 1
      } else {
        await this.store.update<OutboxRecord>(
          key,
          failed(record, result.reason ?? 'the server asked to retry'),
        )
      }
    }
    return { removed, rejected }
  }

  /**
   * A record the server applied: a completion, or a position past the mark
   * (which completes the episode exactly as an online report would), is an
   * accepted completion; an un-mark is an un-mark. `stale` says nothing
   * certain — a stale completion means the user took the mark back later —
   * so it announces nothing.
   */
  private announceApplied(record: OutboxRecord): void {
    if (record.kind === 'completion') {
      this.announceWatched(record.user_id, record.episode_id, true)
    } else if (record.kind === 'unmark') {
      this.announceWatched(record.user_id, record.episode_id, false)
    } else if (
      record.position_s !== undefined &&
      record.duration_s !== undefined &&
      crossesCompletion(record.position_s, record.duration_s)
    ) {
      this.announceWatched(record.user_id, record.episode_id, true)
    }
  }

  private async add(
    kind: Exclude<RecordKind, 'position'>,
    userId: number,
    episodeId: number,
    at: string,
    position?: number,
    duration?: number,
  ): Promise<void> {
    const record: OutboxRecord = {
      id: this.newId(),
      kind,
      user_id: userId,
      episode_id: episodeId,
      at,
      seq: await this.nextSeq(),
      first_at: at,
      attempts: 0,
    }
    if (position !== undefined && duration !== undefined) {
      record.position_s = Math.max(0, position)
      record.duration_s = duration
    }
    await this.store.put(keyOf(record), record)
  }

  /** Whether a completion for this episode is queued and not undone by a later un-mark. */
  private async completionQueued(userId: number, episodeId: number): Promise<boolean> {
    let queued = false
    for (const record of await this.all()) {
      if (record.user_id !== userId || record.episode_id !== episodeId) continue
      if (record.problem !== undefined) continue
      if (record.kind === 'completion') queued = true
      if (record.kind === 'unmark') queued = false
    }
    return queued
  }

  /**
   * The next number in this device's order. The highest stored `seq` is read
   * once, behind one shared promise, so two first calls cannot both start
   * from it; every call after chains on the previous one.
   */
  private nextSeq(): Promise<number> {
    const previous =
      this.seq ??
      this.all().then((records) => records.reduce((max, record) => Math.max(max, record.seq), 0))
    const next = previous.then((value) => value + 1)
    this.seq = next
    return next
  }
}

/** The app's one outbox: two would race each other for the same records. */
let shared: Outbox | null = null

export function outbox(): Outbox {
  shared ??= new Outbox()
  return shared
}

/** Replace the shared outbox. Tests only. */
export function setOutbox(replacement: Outbox | null): void {
  shared = replacement
}

/**
 * Wire the flush triggers once, at the app shell. Returns an unwire function.
 *
 * `visibilitychange` to hidden and `pagehide` are both listened for: iOS fires
 * `pagehide` when a tab or home-screen app is swiped away and only
 * `visibilitychange` when it goes to the background. `online` and the
 * observed network flipping back (`subscribe`, from `@/offline/network`) are
 * the reconnect.
 */
export function startFlushing(
  box: Outbox,
  network: { subscribe: (listener: () => void) => () => void; offline: () => boolean },
): () => void {
  const flush = (keepalive = false) => {
    void box.flush({ keepalive })
  }
  const tick = setInterval(() => {
    flush()
  }, FLUSH_INTERVAL_MS)
  const onVisibility = () => {
    if (document.visibilityState === 'hidden') flush(true)
  }
  const onHide = () => {
    flush(true)
  }
  const onOnline = () => {
    flush()
  }
  const unsubscribe = network.subscribe(() => {
    if (!network.offline()) flush()
  })
  const unpersist = onPersistenceLost(() => {
    void box.refresh()
  })

  document.addEventListener('visibilitychange', onVisibility)
  window.addEventListener('pagehide', onHide)
  window.addEventListener('online', onOnline)
  flush()

  return () => {
    clearInterval(tick)
    unsubscribe()
    unpersist()
    document.removeEventListener('visibilitychange', onVisibility)
    window.removeEventListener('pagehide', onHide)
    window.removeEventListener('online', onOnline)
  }
}
