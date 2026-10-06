/**
 * Trips keep themselves (spec FR-A12, FR-S9, architecture §5.4d, M19 T6).
 *
 * Mounted once by the signed-in shell (`RequireAuth`), so it runs on every
 * page, the player included. It reads `GET /api/trips/current` on mount, every
 * {@link TRIP_POLL_MS} while Arc is open, when Arc comes back on screen or
 * back online, and when the live stream says a copy or an episode moved
 * (`lib/events.ts` marks the query stale). For each answer, {@link autoKeep}:
 *
 * - an `available` episode the device has no record of is kept: its small
 *   copy is downloaded through the manager's usual queue
 *   (`keepTripCopy`) — unless the viewer took it off this device by hand,
 *   which only "Ask again" undoes;
 * - an episode the device already has a record of joins the trip
 *   (`adoptTrip`): a copy already on the device — small or full size — is
 *   confirmed to the server at once; a record paused by hand stays paused;
 * - and every trip copy the server has not heard about yet is confirmed again.
 *
 * Not for the demo account, nor where the browser cannot keep files (no OPFS
 * or no workers); nothing runs while nobody is signed in.
 */

import { useEffect } from 'react'
import type { User } from '@/lib/auth'
import { tripCopyUrl, useCurrentTrip, type Trip } from '@/lib/trips'
import { downloads, type DownloadManager } from '@/offline/downloads'
import { canDownloadInApp } from '@/offline/opfs'

/**
 * One pass over the current trip. Pure but for the manager it is handed;
 * never rejects (a start whose payload cannot be had is tried at the next
 * pass).
 */
export async function autoKeep(trip: Trip | null, manager: DownloadManager): Promise<void> {
  await manager.whenHydrated()
  if (manager.ownerId === null) return
  if (trip !== null && trip.state === 'active') {
    for (const episode of trip.episodes) {
      // The server's own URL, only while the copy can be downloaded.
      const url = tripCopyUrl(episode)
      const record = manager.record(episode.episode_id)
      if (record !== undefined) {
        manager.adoptTrip(episode.episode_id, trip.id, url)
        continue
      }
      if (url === null || manager.isDeclined(trip.id, episode.episode_id)) continue
      try {
        await manager.keepTripCopy({ episodeId: episode.episode_id, tripId: trip.id, url })
      } catch {
        // No payload for it right now (no network): the next pass tries again.
      }
    }
  }
  manager.confirmPending()
}

/** Whether this account on this browser keeps trips at all. */
export function keepsTrips(me: User | null | undefined): boolean {
  return me !== undefined && me !== null && !me.is_demo && canDownloadInApp()
}

export function useTripAutoKeep(me: User | null | undefined): void {
  const enabled = keepsTrips(me)
  const { data, dataUpdatedAt } = useCurrentTrip(enabled)
  const trip = data ?? null

  useEffect(() => {
    if (!enabled) return
    void autoKeep(trip, downloads())
    // `dataUpdatedAt` makes every answer a pass, even one equal to the last:
    // a confirmation that failed is retried on each tick.
  }, [enabled, trip, dataUpdatedAt])
}
