/**
 * The React side of the download manager (spec FR-S9).
 *
 * `useDownloadsSession` is called once, by the signed-in shell
 * (`RequireAuth`), beside the outbox's session: it tells the manager who is
 * signed in, reconciles what is on disk with what was recorded, and wires the
 * two automatic recoveries (back on screen, back online). `useDownloads` is
 * what the Show page, the Downloads page and the player read.
 */

import { useEffect, useSyncExternalStore } from 'react'
import {
  downloads,
  type DownloadManager,
  type DownloadRecord,
  type Downloads,
} from '@/offline/downloads'
import { outbox, type Outbox } from '@/offline/outbox'

/**
 * Watched copies leave the device (owner, 2026-10-08): what the outbox
 * announces about completions the server accepted marks the records, and
 * every flush that reached the server is a pass. Returns the unwire.
 */
export function wireWatchedRemoval(box: Outbox, manager: DownloadManager): () => void {
  const unwatched = box.onWatched((userId, episodeId, watched) => {
    void manager.noteWatched(userId, episodeId, watched)
  })
  const unflushed = box.onFlushed(() => {
    void manager.removeWatched()
  })
  return () => {
    unwatched()
    unflushed()
  }
}

export function useDownloadsSession(userId: number | null): void {
  useEffect(() => {
    const manager = downloads()
    manager.setOwner(userId)
    void manager.hydrate()
    if (userId === null) return undefined
    const unlisten = manager.listen()
    const unwire = wireWatchedRemoval(outbox(), manager)
    return () => {
      unwire()
      unlisten()
    }
  }, [userId])
}

/** This device's "Remove episodes once watched" switch. */
export function useRemoveWatched(): boolean {
  const manager = downloads()
  return useSyncExternalStore(manager.subscribe, manager.getRemoveWatched, manager.getRemoveWatched)
}

export function useDownloads(): Downloads {
  const manager = downloads()
  return useSyncExternalStore(manager.subscribe, manager.getSnapshot, manager.getSnapshot)
}

/** One episode's record for the signed-in account, or `undefined`. */
export function useDownload(episodeId: number): DownloadRecord | undefined {
  return useDownloads()[episodeId]
}
