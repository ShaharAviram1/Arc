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

export class EventLog {
  private readonly store: KeyStore
  private readonly now: () => number
  private readonly echo: boolean
  /** Appends in the order they were made. */
  private chain: Promise<void> = Promise.resolve()

  constructor(store: KeyStore, now: () => number = () => Date.now(), echo = false) {
    this.store = store
    this.now = now
    this.echo = echo
  }

  /** Note one decision. Returns at once; the write follows in order. */
  note(kind: string, episodeId?: number, detail?: string): void {
    const event: DeviceEvent = { at: new Date(this.now()).toISOString(), kind }
    if (episodeId !== undefined) event.episode_id = episodeId
    if (detail !== undefined && detail !== '') event.detail = detail.slice(0, DETAIL_MAX)
    if (this.echo) console.info('[arc downloads]', kind, episodeId ?? '', detail ?? '')
    this.chain = this.chain
      .then(() =>
        this.store.update<DeviceEvent[]>(EVENT_LOG_KEY, (current) => {
          const list = Array.isArray(current) ? current : []
          return [...list, event].slice(-EVENT_LOG_MAX)
        }),
      )
      .catch(() => undefined)
  }

  /** Every entry, oldest first, once the appends already made have landed. */
  async entries(): Promise<DeviceEvent[]> {
    await this.chain
    try {
      const found = await this.store.get<DeviceEvent[]>(EVENT_LOG_KEY)
      return Array.isArray(found) ? found : []
    } catch {
      return []
    }
  }
}
