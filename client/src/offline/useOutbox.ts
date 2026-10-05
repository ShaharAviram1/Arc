/**
 * The React side of the progress outbox (FR-S8).
 *
 * `useOutboxSession` is called once, by the signed-in shell (`RequireAuth`):
 * it tells the outbox who is signed in, wires the flush triggers for as long
 * as somebody is, and refreshes the show pages and Watch Now after a flush
 * that the server took something from. `useOutboxProblems` is what Watch
 * Now's failure banner reads (FR-W6); `usePendingWatched` is what the show
 * page's watched control reads to say a mark is waiting to sync.
 */

import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ANIME_QUERY_KEY } from '@/lib/anime'
import { HOME_QUERY_KEY } from '@/lib/schedule'
import { offlineNow, subscribeNetwork, watchNetwork } from '@/offline/network'
import { outbox, startFlushing, type OutboxRecord, type OutboxSnapshot } from '@/offline/outbox'

/** Point the outbox at the signed-in account and keep it flushing. */
export function useOutboxSession(userId: number | null): void {
  const client = useQueryClient()
  useEffect(() => {
    const box = outbox()
    box.setOwner(userId)
    if (userId === null) return undefined
    const unwatch = watchNetwork()
    const stop = startFlushing(box, { subscribe: subscribeNetwork, offline: offlineNow })
    // Records the server took change ticks and list numbers it renders.
    const unflushed = box.onFlushed((outcome) => {
      if (outcome.removed === 0) return
      void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY] })
      void client.invalidateQueries({ queryKey: [HOME_QUERY_KEY] })
    })
    return () => {
      unflushed()
      stop()
      unwatch()
      box.setOwner(null)
    }
  }, [userId, client])
}

function useSnapshot(): OutboxSnapshot {
  const box = outbox()
  return useSyncExternalStore(box.subscribe, box.getSnapshot, box.getSnapshot)
}

export interface OutboxProblems extends OutboxSnapshot {
  /** Delete one rejected record, explicitly. */
  dismiss: (record: OutboxRecord) => void
}

export function useOutboxProblems(): OutboxProblems {
  const snapshot = useSnapshot()
  const dismiss = useCallback((record: OutboxRecord) => {
    void outbox().dismiss(record.id)
  }, [])
  return { ...snapshot, dismiss }
}

/**
 * `episode id → the watched state a queued mark or un-mark will set`, for the
 * signed-in account. The latest of the two per episode wins, in the device's
 * own order — exactly what the replay will leave.
 */
export function pendingWatched(pending: readonly OutboxRecord[]): ReadonlyMap<number, boolean> {
  const out = new Map<number, boolean>()
  for (const record of pending) {
    if (record.kind === 'completion') out.set(record.episode_id, true)
    else if (record.kind === 'unmark') out.set(record.episode_id, false)
  }
  return out
}

export function usePendingWatched(): ReadonlyMap<number, boolean> {
  const { pending } = useSnapshot()
  return useMemo(() => pendingWatched(pending), [pending])
}

/** How many rows `OutboxProblemRows` draws for this snapshot; 0 means nothing to say. */
export function outboxRowCount(problems: OutboxSnapshot): number {
  return (
    problems.problems.length +
    (problems.stuck.count > 0 ? 1 : 0) +
    (problems.foreign > 0 ? 1 : 0) +
    (!problems.persistent && problems.waiting > 0 ? 1 : 0)
  )
}
