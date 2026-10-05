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
import { downloads, type DownloadRecord, type Downloads } from '@/offline/downloads'

export function useDownloadsSession(userId: number | null): void {
  useEffect(() => {
    const manager = downloads()
    manager.setOwner(userId)
    void manager.hydrate()
    if (userId === null) return undefined
    return manager.listen()
  }, [userId])
}

export function useDownloads(): Downloads {
  const manager = downloads()
  return useSyncExternalStore(manager.subscribe, manager.getSnapshot, manager.getSnapshot)
}

/** One episode's record for the signed-in account, or `undefined`. */
export function useDownload(episodeId: number): DownloadRecord | undefined {
  return useDownloads()[episodeId]
}
