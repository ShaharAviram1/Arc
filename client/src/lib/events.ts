/**
 * Live updates: one `EventSource` per tab, so the page stays true (§5.9).
 *
 * Everything a viewer is waiting for happens in the worker — an episode
 * reaching `ready`, a still landing — and until this existed the only way a
 * page found out was by asking again. Which it does: the show page polls while
 * acquisition is in flight (`ACQUISITION_POLL_MS`) and Watch Now does not poll
 * at all, so a "Ready to watch" tile appeared when you navigated somewhere and
 * came back. This closes that gap without turning the app into a poller: the
 * server publishes what changed (`arc/services/events.py`), `GET /api/events`
 * streams it, and this hook turns each event into the one thing a client can
 * do with it — mark the queries it makes stale.
 *
 * Four decisions worth keeping:
 *
 * - **It invalidates, it does not patch.** An event carries ids and nothing
 *   else, deliberately: it is broadcast to every signed-in tab, so it must not
 *   carry anybody's data. The tab re-asks the endpoint it already had, with its
 *   own session, and the server decides what this viewer may see. A patch built
 *   from an event would be this client guessing at an answer only the server
 *   can give (whether *this* user is behind, whether the tile belongs on *this*
 *   Watch Now).
 *
 * - **Bursts coalesce.** A transcode finishing writes one state change, but a
 *   `compute_wants` tick writes a dozen, and a nightly enrichment hundreds. One
 *   invalidation per event would mean one refetch per event. So keys collect
 *   for {@link COALESCE_MS} and go out once.
 *
 * - **A hidden tab stops listening.** Not immediately — switching tabs for ten
 *   seconds should not drop the stream — but after {@link HIDDEN_GRACE_MS} the
 *   connection is closed, because a browser with forty Arc tabs open should not
 *   hold forty streams against a cap of a hundred. Coming back re-opens it and
 *   invalidates once, which is what the stream would have said.
 *
 * - **Polling stays.** Every interval in `anime.ts` is untouched. A stream that
 *   cannot connect, a proxy that buffers it, an event dropped by a full queue —
 *   each costs a viewer some seconds, not correctness. Nothing here is allowed
 *   to be the only way a page learns something.
 */

import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { acquisitionStatusQueryKey } from '@/lib/acquisition'
import { animeQueryKey, ANIME_QUERY_KEY } from '@/lib/anime'
import { useMe } from '@/lib/auth'
import { HOME_QUERY_KEY } from '@/lib/schedule'

/** The stream. Same origin, session cookie, no headers of its own. */
export const EVENTS_PATH = '/api/events'

/** An episode moved through the state machine (spec §6). */
export const EPISODE_STATE_EVENT = 'episode_state'
/** Artwork landed on a show or its episodes (§5.8). */
export const ART_EVENT = 'art'

/**
 * How long keys collect before one round of invalidation goes out. Long enough
 * that a reconciler tick is one refetch, short enough that a single state
 * change still reads as immediate.
 */
export const COALESCE_MS = 300

/**
 * How long a hidden tab keeps its stream. A tab switch is seconds; a tab left
 * in the background is for ever, and each one is a held connection.
 */
export const HIDDEN_GRACE_MS = 60_000

/** What the server publishes. Ids only — see the note above. */
export interface ArcEvent {
  kind: string
  anime_id: number
  episode_id: number | null
  state: string | null
  ts: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function orNull(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

/**
 * One frame's `data`, parsed, or `null` for anything that is not an Arc event.
 *
 * Defensive on purpose: this is a channel anything on the database could write
 * to, and a client that throws inside `onmessage` loses the rest of the stream.
 * A frame missing the two fields that identify it is dropped.
 */
export function parseEvent(data: unknown): ArcEvent | null {
  if (typeof data !== 'string') return null
  let parsed: unknown
  try {
    parsed = JSON.parse(data)
  } catch {
    return null
  }
  if (!isRecord(parsed)) return null
  const kind = parsed.kind
  const animeId = parsed.anime_id
  if (typeof kind !== 'string' || typeof animeId !== 'number') return null
  return {
    kind,
    anime_id: animeId,
    episode_id: orNull(parsed.episode_id),
    state: stringOrNull(parsed.state),
    ts: stringOrNull(parsed.ts) ?? '',
  }
}

/**
 * The queries an event makes stale. Pure, so the mapping is testable without
 * a DOM, and the whole of what this feature decides.
 *
 * An episode state change is visible in three places: the Watch Now shelves
 * (a new "Ready to watch" tile, a "New this week" row), the show's own page
 * (the row that flips to Ready or Downloading), and — for an admin, who is the
 * only one who can see it — the acquisition status line in the account menu.
 * Artwork is visible on the show page alone; the shelves read the same rows,
 * but a still arriving is not worth re-asking every viewer's dashboard for.
 *
 * An unknown kind maps to nothing, so a server that starts publishing a third
 * kind does not make an old client refetch the world.
 */
export function staleKeys(
  event: ArcEvent,
  { isAdmin }: { isAdmin: boolean },
): readonly (readonly unknown[])[] {
  switch (event.kind) {
    case EPISODE_STATE_EVENT: {
      const keys: (readonly unknown[])[] = [[HOME_QUERY_KEY], animeQueryKey(event.anime_id)]
      if (isAdmin) keys.push(acquisitionStatusQueryKey)
      return keys
    }
    case ART_EVENT:
      return [animeQueryKey(event.anime_id)]
    default:
      return []
  }
}

/**
 * Everything the stream could have said while it was closed.
 *
 * Show *details* rather than everything under `ANIME_QUERY_KEY`: search
 * results live under the same prefix and re-running a search costs the server
 * an AniList call, which a returning tab has not earned.
 */
export function invalidateAfterGap(client: QueryClient, isAdmin: boolean): void {
  void client.invalidateQueries({ queryKey: [HOME_QUERY_KEY] })
  void client.invalidateQueries({
    predicate: (query) => {
      const [head, ...rest] = query.queryKey
      return head === ANIME_QUERY_KEY && rest.length === 1
    },
  })
  if (isAdmin) void client.invalidateQueries({ queryKey: acquisitionStatusQueryKey })
}

/**
 * Open the stream for as long as this component is mounted and signed in.
 *
 * Mounted once, in the app shell (`components/Layout.tsx`), because a tab
 * wants exactly one connection: the cap is per api process, and a hook used on
 * three pages would open three.
 *
 * Returns nothing. There is nothing to render from a stream whose only effect
 * is that other queries are newer than they would have been — and a "live"
 * indicator would be a promise this deliberately does not make.
 */
export function useLiveEvents(): void {
  const client = useQueryClient()
  const { data: me } = useMe()
  // `undefined` while `/api/auth/me` is in flight, `null` when signed out.
  const signedIn = me !== undefined && me !== null
  const isAdmin = me?.role === 'admin'

  useEffect(() => {
    if (!signedIn) return
    // jsdom has no `EventSource`, and neither does a very old browser. Both
    // keep the polling they always had.
    if (typeof EventSource === 'undefined') return

    let source: EventSource | null = null
    let hiddenTimer: ReturnType<typeof setTimeout> | null = null
    let flushTimer: ReturnType<typeof setTimeout> | null = null
    // Keyed by the serialised query key, so two events about the same show
    // collapse into one entry rather than two identical invalidations.
    const pending = new Map<string, readonly unknown[]>()

    function flush(): void {
      if (flushTimer !== null) {
        clearTimeout(flushTimer)
        flushTimer = null
      }
      for (const queryKey of pending.values()) void client.invalidateQueries({ queryKey })
      pending.clear()
    }

    function markStale(keys: readonly (readonly unknown[])[]): void {
      if (keys.length === 0) return
      for (const key of keys) pending.set(JSON.stringify(key), key)
      if (flushTimer !== null) return
      flushTimer = setTimeout(flush, COALESCE_MS)
    }

    function onMessage(event: MessageEvent<unknown>): void {
      const parsed = parseEvent(event.data)
      if (parsed === null) return
      markStale(staleKeys(parsed, { isAdmin }))
    }

    function open(): void {
      if (source !== null) return
      source = new EventSource(EVENTS_PATH)
      // No `error` handler: `EventSource` reconnects on its own, with its own
      // backoff, and there is nothing useful for this to do in between.
      source.addEventListener('message', onMessage)
    }

    function close(): void {
      hiddenTimer = null
      // Whatever was waiting on the coalesce window goes out now. Dropping it
      // would mean an event that arrived 200 ms before a tab was hidden (or
      // before a navigation unmounted the shell) is the one event the app
      // never acts on — the keys are already known to be stale, and marking
      // them costs nothing.
      if (pending.size > 0) flush()
      if (source === null) return
      source.removeEventListener('message', onMessage)
      source.close()
      source = null
    }

    function pauseSoon(): void {
      if (source === null || hiddenTimer !== null) return
      hiddenTimer = setTimeout(close, HIDDEN_GRACE_MS)
    }

    function onVisibilityChange(): void {
      if (document.visibilityState === 'hidden') {
        pauseSoon()
        return
      }
      if (hiddenTimer !== null) {
        clearTimeout(hiddenTimer)
        hiddenTimer = null
      }
      if (source !== null) return
      open()
      invalidateAfterGap(client, isAdmin)
    }

    open()
    // A tab can be hidden *before* this ever mounts — opened in the
    // background, or restored by the browser into a window that is not the
    // front one — and `visibilitychange` does not fire for a state that was
    // already true. Without this, such a tab would hold its stream for as
    // long as it stayed hidden, which is the one case the grace timer exists
    // to stop.
    if (document.visibilityState === 'hidden') pauseSoon()
    document.addEventListener('visibilitychange', onVisibilityChange)

    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      if (hiddenTimer !== null) clearTimeout(hiddenTimer)
      close()
    }
  }, [client, signedIn, isAdmin])
}
