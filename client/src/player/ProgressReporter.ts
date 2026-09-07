/**
 * When to tell the server where the viewer is (spec §4.5 FR-S3).
 *
 * Framework-free on purpose: the rules are about time and media events, not
 * about rendering, and they are far easier to test with fake timers than
 * through a component. The player owns an instance, feeds it the video's
 * events, and hands it a `report` function — normally a mutation — for every
 * write except the last one.
 *
 * The last one is different. A tab being closed does not stay alive long
 * enough for `fetch` to finish, so the unload path uses `sendBeacon`, which
 * the browser delivers after the page is gone. Hence the beacon is built here
 * rather than going through the query layer: it must be a fire-and-forget
 * request with a body the server will accept without a preflight, which is
 * what a `Blob` typed `application/json` gives us.
 */

import { PROGRESS_PATH } from '@/lib/playback'

/** While playing (FR-S3). */
export const REPORT_INTERVAL_MS = 10_000

/** Scrubbing fires `seeked` repeatedly; only the position it settles on matters. */
export const SEEK_DEBOUNCE_MS = 500

/**
 * Floor between two ordinary reports. Pause, seek and unload are deliberate
 * moments a viewer would expect to be saved, so they ignore it.
 */
export const MIN_REPORT_GAP_MS = 2_000

export interface ProgressReporterOptions {
  episodeId: number
  /** Sends one report; the player wires this to the progress mutation. */
  report: (position: number, duration: number) => void
  /** Where the unload beacon goes. Overridable for tests. */
  path?: string
}

interface Sample {
  position: number
  duration: number
}

function usable(value: number): boolean {
  return Number.isFinite(value) && value >= 0
}

/**
 * Whether the server would take this sample. `POST /api/progress` requires a
 * duration greater than zero and answers 422 otherwise, so a sample taken
 * before the media reported its length is held back rather than thrown at the
 * API — the next `timeupdate` that carries a duration makes it sendable, and
 * the position is not lost in the meantime.
 */
function sendable(sample: Sample): boolean {
  return sample.duration > 0
}

export class ProgressReporter {
  private readonly episodeId: number
  private readonly report: (position: number, duration: number) => void
  private readonly path: string

  /** The most recent position/duration the player told us about. */
  private sample: Sample | null = null
  private lastReportedAt = 0
  private lastReportedPosition: number | null = null

  private interval: ReturnType<typeof setInterval> | null = null
  private seekTimer: ReturnType<typeof setTimeout> | null = null
  /** Set once the media ends: the final report has been sent, so we go quiet. */
  private stopped = false

  private readonly onPageHide = () => {
    this.flush()
  }

  constructor(options: ProgressReporterOptions) {
    this.episodeId = options.episodeId
    this.report = options.report
    this.path = options.path ?? PROGRESS_PATH
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', this.onPageHide)
    }
  }

  /**
   * The last position the player told us about, or null before the first one.
   * The page reads it to put a recovered stream back where it was.
   */
  get lastPosition(): number | null {
    return this.sample?.position ?? null
  }

  /** From `timeupdate`: keeps the position the timer will eventually report. */
  update(position: number, duration: number): void {
    if (this.stopped || !usable(position)) return
    const known = usable(duration) && duration > 0 ? duration : (this.sample?.duration ?? 0)
    this.sample = { position, duration: known }
  }

  /**
   * Starting playback is worth saving — it is where a resumed session's real
   * position first settles — but only as an ordinary report, so a pause
   * immediately followed by a play does not send two writes a moment apart.
   */
  play(position: number, duration: number): void {
    if (this.stopped) return
    this.update(position, duration)
    if (this.interval === null) {
      this.interval = setInterval(() => {
        this.emit(false)
      }, REPORT_INTERVAL_MS)
    }
    this.emit(false)
  }

  pause(position: number, duration: number): void {
    if (this.stopped) return
    this.update(position, duration)
    this.stopTicking()
    this.cancelSeek()
    this.emit(true)
  }

  /** A settled seek is worth saving; a scrub in progress is not. */
  seek(position: number, duration: number): void {
    if (this.stopped) return
    this.update(position, duration)
    this.cancelSeek()
    this.seekTimer = setTimeout(() => {
      this.seekTimer = null
      this.emit(true)
    }, SEEK_DEBOUNCE_MS)
  }

  /** The final report. Everything after it is ignored (FR-S4 has landed). */
  end(position: number, duration: number): void {
    if (this.stopped) return
    this.update(position, duration)
    this.stopTicking()
    this.cancelSeek()
    this.emit(true)
    this.stopped = true
  }

  /**
   * The unload path: a beacon, because the page is on its way out. A position
   * the server already has is not worth a request the browser has to keep
   * alive past the page — the same rule `destroy` applies.
   */
  flush(): void {
    const sample = this.sample
    if (this.stopped || sample === null || sample.position === this.lastReportedPosition) return
    this.beacon(sample)
  }

  /**
   * Unmounting is the "close" of FR-S3 as far as a single-page app is
   * concerned, so a position the server has not seen still goes out — but as a
   * beacon, since the React tree that owns the mutation is being torn down.
   */
  destroy(): void {
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', this.onPageHide)
    }
    this.stopTicking()
    this.cancelSeek()
    const sample = this.sample
    if (!this.stopped && sample !== null && sample.position !== this.lastReportedPosition) {
      this.beacon(sample)
    }
    this.stopped = true
  }

  private stopTicking(): void {
    if (this.interval === null) return
    clearInterval(this.interval)
    this.interval = null
  }

  private cancelSeek(): void {
    if (this.seekTimer === null) return
    clearTimeout(this.seekTimer)
    this.seekTimer = null
  }

  /** Returns whether anything was actually sent, which the tests lean on. */
  private emit(force: boolean): boolean {
    const sample = this.sample
    if (sample === null || !sendable(sample)) return false
    const now = Date.now()
    if (!force && now - this.lastReportedAt < MIN_REPORT_GAP_MS) return false
    this.mark(now, sample.position)
    this.report(sample.position, sample.duration)
    return true
  }

  private mark(at: number, position: number): void {
    this.lastReportedAt = at
    this.lastReportedPosition = position
  }

  private beacon(sample: Sample): void {
    if (!sendable(sample)) return
    const payload = JSON.stringify({
      episode_id: this.episodeId,
      position_s: sample.position,
      duration_s: sample.duration,
    })
    this.mark(Date.now(), sample.position)

    // `sendBeacon` answers false when the browser refuses to queue the request
    // (over the per-page byte budget, say); a keepalive fetch is the fallback,
    // and is also what a browser without `sendBeacon` at all gets.
    try {
      if (typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
        const blob = new Blob([payload], { type: 'application/json' })
        if (navigator.sendBeacon(this.path, blob)) return
      }
    } catch {
      // Fall through to fetch.
    }

    try {
      void fetch(this.path, {
        method: 'POST',
        body: payload,
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        keepalive: true,
      }).catch(() => undefined)
    } catch {
      // Nothing left to try; a lost final report costs at most ten seconds.
    }
  }
}
