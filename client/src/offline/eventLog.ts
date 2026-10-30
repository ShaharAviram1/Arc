/**
 * The download manager's own diary (M20.4 E, owner 2026-10-30): what it
 * decided about the copies on this device, and why — so that when a copy
 * goes missing there is something to read.
 *
 * A ring buffer of at most {@link EVENT_LOG_MAX} entries, kept in IndexedDB
 * `meta` under {@link EVENT_LOG_KEY} (no new object store, so no schema
 * upgrade), appended in order through the store's transactional `update`.
 * Each entry is a time, a kind, maybe an episode id and maybe a short detail:
 * states and reasons, file sizes, the server's status codes. Never a name, an
 * account or a title. The Downloads page's "Send report" posts the whole of it
 * to `POST /api/devices/{id}/report`; in development each entry is also
 * written to the console.
 *
 * Like everything over the store, it never rejects: a log that cannot be
 * written is a log with a gap, never a broken download.
 */

import type { KeyStore } from '@/offline/store'

/** One decision, as the server stores it (`{at, kind, episode_id?, detail?}`). */
export interface DeviceEvent {
  /** ISO 8601. */
  at: string
  /** `reconcile`, `state`, `truncated`, `dropped`, `replace`, `server`, `reask`, `holdings`, … */
  kind: string
  episode_id?: number
  detail?: string
}

/** The most entries kept; the oldest go first. Also the most one report carries. */
export const EVENT_LOG_MAX = 500

/** Where the log lives in `meta`. */
export const EVENT_LOG_KEY = 'event-log'

/** The longest detail kept, so one odd error message cannot crowd out the rest. */
const DETAIL_MAX = 300

/** How long appends gather before one write (a burst of decisions is one transaction). */
export const EVENT_LOG_FLUSH_MS = 1_000

/** What is stored: the event, and locally only, the account it concerns. */
interface StoredEvent extends DeviceEvent {
  /** The signed-in account when it was noted; never sent ({@link EventLog.entries}). */
  user?: number
}

export class EventLog {
  private readonly store: KeyStore
  private readonly now: () => number
  private readonly echo: boolean
  /** Appends not yet written, oldest first. */
  private pending: StoredEvent[] = []
  private timer: ReturnType<typeof setTimeout> | null = null
  /** Writes in the order they were made. */
  private chain: Promise<void> = Promise.resolve()

  constructor(store: KeyStore, now: () => number = () => Date.now(), echo = false) {
    this.store = store
    this.now = now
    this.echo = echo
  }

  /**
   * Note one decision, for `user` (the signed-in account, kept on the device
   * only so a shared iPad's report covers one account). Returns at once; the
   * write follows within {@link EVENT_LOG_FLUSH_MS}, in order.
   */
  note(kind: string, episodeId?: number, detail?: string, user?: number | null): void {
    const event: StoredEvent = { at: new Date(this.now()).toISOString(), kind }
    if (episodeId !== undefined) event.episode_id = episodeId
    if (detail !== undefined && detail !== '') event.detail = detail.slice(0, DETAIL_MAX)
    if (user !== undefined && user !== null) event.user = user
    if (this.echo) console.info('[arc downloads]', kind, episodeId ?? '', detail ?? '')
    this.pending.push(event)
    this.timer ??= setTimeout(() => {
      void this.flush()
    }, EVENT_LOG_FLUSH_MS)
  }

  /** Write what is pending now, in one update. */
  private flush(): Promise<void> {
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
    const batch = this.pending
    this.pending = []
    if (batch.length > 0) {
      this.chain = this.chain
        .then(() =>
          this.store.update<StoredEvent[]>(EVENT_LOG_KEY, (current) => {
            const list = Array.isArray(current) ? current : []
            return [...list, ...batch].slice(-EVENT_LOG_MAX)
          }),
        )
        .catch(() => undefined)
    }
    return this.chain
  }

  /**
   * The entries, oldest first, once every append made so far has landed:
   * with `user`, only those noted for that account or for no account (the
   * device's own, such as the launch sweep). The account is never in them.
   */
  async entries(user?: number | null): Promise<DeviceEvent[]> {
    await this.flush()
    let found: StoredEvent[] | undefined
    try {
      found = await this.store.get<StoredEvent[]>(EVENT_LOG_KEY)
    } catch {
      return []
    }
    if (!Array.isArray(found)) return []
    return found
      .filter((event) => user === undefined || event.user === undefined || event.user === user)
      .map((event) => {
        const sent: DeviceEvent = { at: event.at, kind: event.kind }
        if (event.episode_id !== undefined) sent.episode_id = event.episode_id
        if (event.detail !== undefined) sent.detail = event.detail
        return sent
      })
  }
}
