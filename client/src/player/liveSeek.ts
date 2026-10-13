/**
 * Seeking in a stream that is still being cut from a download (spec FR-P8,
 * FR-S2, FR-S6; architecture §5.4a, M20 T7). Pure: no element, no React.
 *
 * The playable part of a live stream ends at the **download front** — the
 * last segment the server has written. A seek past it cannot be served, and
 * neither hls.js nor Safari says so politely: one stalls on a spinner, the
 * other may jump to whatever it calls the live edge. So the player never asks
 * for a position past the front. A seek that lands beyond `front − 1 s`
 * becomes a **pending seek**: the playhead goes to just short of the front
 * now, the viewer is told where it will go, and it jumps there once the front
 * is {@link JUMP_LEAD_S} seconds past the target — enough to start playing
 * without stalling on the very next segment.
 */

import { formatClock } from '@/lib/playback'

/** How far short of the front the playhead is put: the last second may not be whole. */
export const FRONT_MARGIN_S = 1

/** How far past a pending target the front must be before the jump. */
export const JUMP_LEAD_S = 6

export interface SeekPlan {
  /** Where the playhead goes now. */
  now: number
  /** Where it goes once the front allows, or null when `now` is the target. */
  pending: number | null
}

/** The furthest point the playhead may be put at while the front is `front`. */
export function playableEdge(front: number): number {
  return Math.max(0, front - FRONT_MARGIN_S)
}

/**
 * A seek to `target` with the front at `front` and the playhead at `current`:
 * immediate when it is at or below `max(front − 1 s, current)` — the playhead
 * has been there, so it is reachable even when the element's range says
 * otherwise (Safari keeps its seekable end three target durations behind the
 * playlist while it plays past it, and reports no range at all at first) —
 * otherwise to that edge now (never backwards) and to `target` later.
 * `planSeek(750, 300)` → `{ now: 299, pending: 750 }`.
 */
export function planSeek(target: number, front: number, current = 0): SeekPlan {
  const wanted = Number.isFinite(target) ? Math.max(0, target) : 0
  const here = Number.isFinite(current) ? Math.max(0, current) : 0
  const edge = Math.max(playableEdge(Number.isFinite(front) ? front : 0), here)
  if (wanted <= edge) return { now: wanted, pending: null }
  return { now: edge, pending: wanted }
}

/**
 * A seek that must not wait (the arrow keys and End, FR-S6): `target`, held to
 * the playable edge (`front − 1 s`, or the playhead if it is further). A
 * playhead already past the edge is left where it is rather than pulled back.
 */
export function clampToFront(target: number, front: number, current = 0): number {
  const wanted = Math.max(0, target)
  const edge = Math.max(playableEdge(front), current)
  if (wanted <= edge) return wanted
  return edge
}

/** FR-S4's completion mark, mirrored for the stale-duration rule below. */
const COMPLETION_FRACTION = 0.9

/**
 * The duration the page measures by (FR-S4, M20 T7). While `live`, the
 * server's (`fallback`). On a page that has seen `live` (`seenExpected`, the
 * expected duration it carried) and no longer does — adopted between two
 * polls — hls.js may still hold the partial playlist and report a stale,
 * short duration: the element's is taken only once it is at least 90 % of
 * the expected one, and the larger of the two until then, so no completion
 * (and no MAL write) can come from a truncated duration. Otherwise the
 * media's own finite positive duration, else `fallback`.
 */
export function measureDuration(
  media: number,
  fallback: number,
  state: { live: boolean; seenExpected: number | null },
): number {
  if (state.live) return fallback
  const known = Number.isFinite(media) && media > 0 ? media : 0
  const seen = state.seenExpected
  if (seen !== null && seen > 0) {
    return known >= COMPLETION_FRACTION * seen ? known : Math.max(known, seen)
  }
  return known > 0 ? known : fallback
}

export type PendingVerdict =
  /** Not yet: the front is short of `target + 6 s`. */
  | { action: 'wait' }
  /** Seek to `at` and forget the pending target. */
  | { action: 'jump'; at: number }
  /** Playback reached the target on its own: forget it, do not seek. */
  | { action: 'drop' }

/**
 * What to do with a pending seek to `target`, now that the front is `front`,
 * the playhead is at `position` and the download has (`ended`) or has not
 * finished. `front` is what the element can reach, not what the server has.
 *
 * A finished download no longer needs the lead — nothing more is coming — so
 * it jumps once the element's front is within six seconds of the target, held
 * to that front. That settles a target in the last seconds of the episode
 * (the expected duration can be a little longer than the file), and it still
 * waits while the element has not reloaded its playlist since the end (its
 * front is then far behind, or 0).
 *
 * `finalFront` is the length the finished download really has (the server's
 * last `front_seconds`, or the adopted rendition's duration). A target past
 * it — the expected duration can be longer than the file by more than six
 * seconds — resolves to the end once the element has caught up with it,
 * rather than staying pending for ever.
 */
export function settlePending(
  target: number,
  state: { front: number; position: number; ended: boolean; finalFront?: number | null },
): PendingVerdict {
  if (state.position >= target) return { action: 'drop' }
  if (state.front >= target + JUMP_LEAD_S) return { action: 'jump', at: target }
  if (!state.ended || state.front <= 0) return { action: 'wait' }
  if (state.front >= target - JUMP_LEAD_S) {
    return { action: 'jump', at: Math.min(target, playableEdge(state.front)) }
  }
  const final = state.finalFront ?? null
  if (final !== null && final > 0 && state.front >= final - FRONT_MARGIN_S) {
    return { action: 'jump', at: playableEdge(state.front) }
  }
  return { action: 'wait' }
}

/**
 * The front as the element knows it: the end of its last seekable range (the
 * playlist hls.js or Safari has loaded), or `fallback` while the element has
 * none (0 for planning a seek; the server's `front_seconds` for drawing the
 * bar). Wrapped, like the buffered
 * read: an element that has loaded nothing may answer with an empty range or
 * throw.
 */
export function readFront(ranges: TimeRanges | null | undefined, fallback: number): number {
  try {
    if (ranges === null || ranges === undefined || ranges.length === 0) return fallback
    const end = ranges.end(ranges.length - 1)
    return Number.isFinite(end) && end > 0 ? end : fallback
  } catch {
    return fallback
  }
}

/**
 * What the player says while the viewer waits on the download (FR-P8):
 * "Downloading — 43 % · 12 seeders · will jump to 12:30", without the jump
 * when nothing is pending and without the seeders before there is a count.
 */
export function liveStatusLabel(
  live: { download_progress: number; seeders: number | null },
  pending: number | null,
): string {
  const percent = Math.floor(Math.max(0, Math.min(1, live.download_progress)) * 100 + 1e-9)
  const parts = [`Downloading — ${String(percent)} %`]
  if (live.seeders !== null) {
    parts.push(live.seeders === 1 ? '1 seeder' : `${String(live.seeders)} seeders`)
  }
  if (pending !== null) parts.push(`will jump to ${formatClock(pending)}`)
  return parts.join(' · ')
}

/**
 * Below this many seeders the player says the download may be slow. The
 * admin's `min_seeders_warn` (FR-A16) is not in the play payload, so the
 * player uses that setting's default rather than a second request.
 */
export const FEW_SEEDERS = 3

export function fewSeeders(seeders: number | null): boolean {
  return seeders !== null && seeders < FEW_SEEDERS
}
