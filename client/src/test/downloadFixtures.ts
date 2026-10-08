/**
 * A download manager with nothing real behind it (FR-S9): a fake worker that
 * records commands and can answer, a map for the files on "disk", and a
 * memory store for the records. Shared by the manager, player and page tests.
 *
 * The server's small copy (M19) is answered `unavailable` unless a test says
 * otherwise, so a plain start downloads the full-size `download_url` exactly
 * as M18 did; the small-copy tests hand in their own `requestCopy`.
 */

import type { OfflineCopyOut } from '@/lib/anime'
import type { PlayInfo } from '@/lib/playback'
import type { WorkerCommand, WorkerMessage } from '@/offline/download'
import {
  DownloadManager,
  type DownloadRecord,
  type ManagerOptions,
  type WorkerLike,
} from '@/offline/downloads'
import { fileNameFor } from '@/offline/opfs'
import { memoryStore, type KeyStore } from '@/offline/store'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'

export class FakeWorker implements WorkerLike {
  commands: WorkerCommand[] = []
  terminated = false
  private listener: ((message: WorkerMessage) => void) | null = null
  private errorListener: ((reason: string) => void) | null = null

  postMessage(message: WorkerCommand): void {
    this.commands.push(message)
  }

  onMessage(listener: (message: WorkerMessage) => void): void {
    this.listener = listener
  }

  onError(listener: (reason: string) => void): void {
    this.errorListener = listener
  }

  terminate(): void {
    this.terminated = true
  }

  emit(message: WorkerMessage): void {
    this.listener?.(message)
  }

  /** The worker failed to load, or died. */
  fail(reason = 'failed to load'): void {
    this.errorListener?.(reason)
  }

  lastCommand(): WorkerCommand | undefined {
    return this.commands[this.commands.length - 1]
  }
}

export interface ManagerHarness {
  manager: DownloadManager
  /** The first worker booted; `workers` holds every one, in boot order. */
  worker: FakeWorker
  workers: FakeWorker[]
  store: KeyStore
  /** File name → bytes on "disk". */
  files: Map<string, number>
  removed: string[]
  covers: { kept: number[]; dropped: number[] }
  /** How many times the live blob URL was revoked. */
  revokes: { count: number }
}

/** The server's answer when it has no small copy to give: download the full file. */
export const NO_SMALL_COPY: OfflineCopyOut = {
  state: 'unavailable',
  progress: null,
  size: null,
  url: null,
  codecs: null,
}

/** A small copy ready to download, as the server would describe it. */
export function smallCopy(episodeId: number, patch: Partial<OfflineCopyOut> = {}): OfflineCopyOut {
  return {
    state: 'available',
    progress: null,
    size: 210_000_000,
    url: `/media/${String(episodeId)}/offline.mp4`,
    codecs: 'avc1.640028',
    ...patch,
  }
}

export const INFO_BY_ID: Record<number, PlayInfo> = {
  9001: PLAY_INFO,
  9002: PLAY_INFO_EPISODE_2,
}

export function managerHarness(
  options: Partial<ManagerOptions> & { initial?: Iterable<[string, unknown]> } = {},
): ManagerHarness {
  const worker = new FakeWorker()
  const workers: FakeWorker[] = []
  const revokes = { count: 0 }
  const store = options.store ?? memoryStore(options.initial)
  const files = new Map<string, number>()
  const removed: string[] = []
  const covers = { kept: [] as number[], dropped: [] as number[] }
  const manager = new DownloadManager({
    store,
    createWorker: () => {
      const next = workers.length === 0 ? worker : new FakeWorker()
      workers.push(next)
      return next
    },
    listFiles: () => Promise.resolve([...files.keys()]),
    revokeUrl: () => {
      revokes.count += 1
    },
    sizeOf: (name) => Promise.resolve(files.get(name) ?? 0),
    removeFrom: (name) => {
      removed.push(name)
      files.delete(name)
      return Promise.resolve()
    },
    blobUrl: (name) => Promise.resolve(files.has(name) ? `blob:${name}` : null),
    persist: () => Promise.resolve(true),
    loadInfo: (episodeId) => {
      const info = INFO_BY_ID[episodeId]
      return info === undefined
        ? Promise.reject(new Error('no such episode'))
        : Promise.resolve(info)
    },
    keepCover: (_userId, animeId) => {
      covers.kept.push(animeId)
      return Promise.resolve()
    },
    dropCover: (_userId, animeId) => {
      covers.dropped.push(animeId)
      return Promise.resolve()
    },
    now: () => Date.parse('2026-10-05T12:00:00Z'),
    requestCopy: () => Promise.resolve(NO_SMALL_COPY),
    pollCopy: () => Promise.resolve(NO_SMALL_COPY),
    canPlayType: () => 'probably',
    isVisible: () => true,
    // Trips (M19 T6): the server always hears, and nothing is remembered on disk.
    confirmDelivered: () => Promise.resolve(null),
    releaseDelivered: () => Promise.resolve(null),
    notes: memoryStore(),
    // Nothing waits in the outbox unless a test says so.
    queuedFor: () => Promise.resolve(false),
    // The server knows of no completion unless a test says so; always reachable.
    loadCompleted: () => Promise.resolve([]),
    isOnline: () => true,
    ...options,
  })
  return { manager, worker, workers, store, files, removed, covers, revokes }
}

/** A finished download record, as the store would hold it. */
export function downloadedRecord(
  userId: number,
  info: PlayInfo = PLAY_INFO,
  bytes = 1000,
): DownloadRecord {
  return {
    userId,
    episodeId: info.episode.id,
    animeId: info.anime.id,
    name: fileNameFor(info.episode.id),
    url: `/media/${String(info.episode.id)}/episode.mp4`,
    variant: 'full',
    state: 'downloaded',
    bytes,
    total: bytes,
    etag: '"e"',
    reason: null,
    message: null,
    snapshot: { anime: info.anime, episode: info.episode, duration: info.duration },
    created_at: '2026-10-04T10:00:00Z',
    updated_at: '2026-10-04T10:00:00Z',
  }
}

export function recordEntry(record: DownloadRecord): [string, DownloadRecord] {
  return [`${String(record.userId)}:${String(record.episodeId)}`, record]
}
