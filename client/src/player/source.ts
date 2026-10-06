/**
 * Where the player's bytes come from (spec FR-S9, architecture §5.4d).
 *
 * A downloaded episode plays **from its file on this device**, online or not:
 * a blob URL straight onto the `<video>` element. The file is one fragmented
 * MP4, which every browser plays natively, so this path bypasses hls.js
 * entirely. Everything else streams as it always did.
 *
 * The URL is resolved **before** the video element mounts, never inside a
 * tap. On iOS an `await` between a user's tap and `video.play()` costs the
 * user gesture, so every press of play in the player calls `play()`
 * synchronously on an element whose `src` is already set.
 */

import { useEffect, useState } from 'react'
import type { PlayInfo } from '@/lib/playback'
import { downloads, type DownloadManager } from '@/offline/downloads'

export type PlaySource =
  | { kind: 'file'; url: string }
  | { kind: 'stream'; url: string }
  /** Rebuilt from a download whose file then could not be read: nothing to play. */
  | { kind: 'none' }

/**
 * Pure: the file when there is one, else the stream when there is one. An
 * `offline_only` episode (M19) and a payload rebuilt from a download have no
 * stream; a null or empty `playlist_url` never becomes a source.
 */
export function chooseSource(info: PlayInfo, localUrl: string | null): PlaySource {
  if (localUrl !== null) return { kind: 'file', url: localUrl }
  const playlist = info.playlist_url
  if (
    info.from_device === true ||
    info.offline_only === true ||
    playlist === null ||
    playlist === ''
  ) {
    return { kind: 'none' }
  }
  return { kind: 'stream', url: playlist }
}

export interface LocalCopy {
  /** False until the device has been asked; the player waits for it. */
  checked: boolean
  url: string | null
  /** The file would not play: drop it and stream instead (when there is a stream). */
  fail: () => void
}

/**
 * The blob URL for this account's download of `episodeId`, or `null`.
 * Answers synchronously when the URL was minted already (`playableUrlNow`),
 * so coming back to a just-played episode does not flash a loading state.
 */
export function useLocalCopy(episodeId: number, manager: DownloadManager = downloads()): LocalCopy {
  const [state, setState] = useState<{ id: number; checked: boolean; url: string | null }>(() => {
    const now = manager.playableUrlNow(episodeId)
    return { id: episodeId, checked: now !== null, url: now }
  })

  useEffect(() => {
    let cancelled = false
    void (async () => {
      await manager.whenHydrated()
      const url = await manager.playableUrl(episodeId)
      if (!cancelled) setState({ id: episodeId, checked: true, url })
    })()
    return () => {
      cancelled = true
    }
  }, [episodeId, manager])

  const current = state.id === episodeId ? state : { checked: false, url: null }
  return {
    checked: current.checked,
    url: current.url,
    fail: () => {
      // Not re-minted: the record becomes `failed` / `unreadable` with a Try
      // again, and the page streams when there is a stream (FR-S9).
      manager.markUnplayable(episodeId)
      setState({ id: episodeId, checked: true, url: null })
    },
  }
}
