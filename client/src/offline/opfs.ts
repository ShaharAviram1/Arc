/**
 * The on-device file store for episodes kept inside Arc (spec FR-S9,
 * architecture §5.4d). Ported from Audiosey, whose M0 spike settled it on an
 * iPhone: OPFS plus a blob URL.
 *
 * One file per episode *and copy* in the origin-private file system, named
 * from the numeric id and nothing else: `episode-<id>.mp4` for the full-size
 * file (FR-S7) and `episode-<id>-o.mp4` for the server's smaller copy made for
 * devices (FR-S9, M19). The two names are load-bearing: two accounts on one
 * iPad may keep the two different copies of one episode, and with one name a
 * download of the small copy would resume into (and then truncate) the other
 * account's full one. The main thread only
 * ever reads here: the writing is a sync access handle inside
 * `downloadWorker.ts`, because that API exists only in a worker.
 *
 * **Playback is `URL.createObjectURL(file.slice(...))`.** It is disk-backed —
 * nothing is copied into memory, which matters for a 400 MB episode on an
 * iPad — and it keeps the service worker out of the media path entirely, as
 * the PWA rule requires.
 *
 * **One live URL at a time.** A live object URL pins the file; leaking one per
 * episode is how an app ends up unable to delete anything. Minting a new one
 * revokes the last.
 *
 * Everything here answers a safe value rather than throwing. OPFS is missing in
 * jsdom, in some private windows and on browsers we have not met, and the app
 * then hides the in-app download and streams as before.
 */

export const EPISODE_MIME = 'video/mp4'

/** Which of the server's two files a download is: the small copy, or the full-size one. */
export type CopyVariant = 'small' | 'full'

/**
 * The file for an episode's copy. Derived from the id and the variant — never
 * from anything typed.
 */
export function fileNameFor(episodeId: number, variant: CopyVariant = 'full'): string {
  const suffix = variant === 'small' ? '-o' : ''
  return `episode-${String(Math.trunc(episodeId))}${suffix}.mp4`
}

export function hasOpfs(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    typeof navigator.storage !== 'undefined' &&
    typeof navigator.storage.getDirectory === 'function'
  )
}

/**
 * Whether this browser can keep an episode inside Arc: OPFS for the bytes and
 * a module worker to write them. The sync access handle itself is exposed only
 * inside a worker, so it cannot be probed from here; a browser that has OPFS
 * and workers but not the handle fails its first download with a sentence.
 */
export function canDownloadInApp(): boolean {
  return hasOpfs() && typeof Worker !== 'undefined'
}

async function root(): Promise<FileSystemDirectoryHandle | null> {
  if (!hasOpfs()) return null
  try {
    return await navigator.storage.getDirectory()
  } catch {
    return null
  }
}

/** The bytes already on the device. `0` for a file that is not there. */
export async function fileSize(name: string): Promise<number> {
  const directory = await root()
  if (directory === null) return 0
  try {
    const handle = await directory.getFileHandle(name)
    return (await handle.getFile()).size
  } catch {
    return 0
  }
}

export async function readFile(name: string): Promise<File | null> {
  const directory = await root()
  if (directory === null) return null
  try {
    return await (await directory.getFileHandle(name)).getFile()
  } catch {
    return null
  }
}

/** `episode-<digits>.mp4` and `episode-<digits>-o.mp4`: the only names Arc ever writes. */
export const EPISODE_FILE = /^episode-\d+(?:-o)?\.mp4$/

/**
 * Every episode file in the origin-private file system, for the launch-time
 * sweep of files no record names (an orphan of an interrupted delete).
 */
export async function listEpisodeFiles(): Promise<string[]> {
  const directory = await root()
  if (directory === null) return []
  const names: string[] = []
  try {
    // `keys()` is in the spec and in every engine with OPFS, but not in the
    // DOM lib this project compiles against.
    const iterable = directory as unknown as { keys(): AsyncIterable<string> }
    for await (const name of iterable.keys()) {
      if (EPISODE_FILE.test(name)) names.push(name)
    }
  } catch {
    // Unreadable directory: sweep nothing rather than guess.
  }
  return names
}

export async function removeFile(name: string): Promise<void> {
  const directory = await root()
  if (directory === null) return
  try {
    await directory.removeEntry(name)
  } catch {
    // Not there — which is the state the caller wanted.
  }
}

let current: { url: string; name: string } | null = null

/**
 * A playable URL for an episode on the device, revoking whatever was playing.
 * `null` when the file is not there: the caller streams instead.
 */
export async function blobUrlFor(name: string): Promise<string | null> {
  const file = await readFile(name)
  if (file === null || file.size === 0) return null
  revokeCurrent()
  const url = URL.createObjectURL(file.slice(0, file.size, EPISODE_MIME))
  current = { url, name }
  return url
}

export function revokeCurrent(): void {
  if (current === null) return
  URL.revokeObjectURL(current.url)
  current = null
}

/**
 * What to call this device in a sentence: "iPad", "iPhone", else "device".
 * iPadOS says "Macintosh" in its user agent, so a Mac-looking browser with a
 * touch screen is taken for an iPad.
 */
export function deviceName(): 'iPad' | 'iPhone' | 'device' {
  try {
    const agent = navigator.userAgent
    if (/iPhone|iPod/.test(agent)) return 'iPhone'
    if (/iPad/.test(agent) || (/Macintosh/.test(agent) && navigator.maxTouchPoints > 1)) {
      return 'iPad'
    }
  } catch {
    // No navigator: say "device".
  }
  return 'device'
}

/**
 * What the Downloads page shows: the browser's own figures. On iPadOS these
 * are the browser's allowance, not the iPad's free space — a full iPad can
 * still report gigabytes to spare (owner, 2026-10-07) — so they are shown as
 * what the browser reports, and never decide whether a download may run.
 */
export async function estimate(): Promise<{ usage: number; quota: number } | null> {
  try {
    if (typeof navigator.storage?.estimate !== 'function') return null
    const found = await navigator.storage.estimate()
    return { usage: found.usage ?? 0, quota: found.quota ?? 0 }
  } catch {
    return null
  }
}

/** Whether the browser has promised not to evict Arc's storage. */
export async function persisted(): Promise<boolean | null> {
  try {
    if (typeof navigator.storage?.persisted !== 'function') return null
    return await navigator.storage.persisted()
  } catch {
    return null
  }
}

/**
 * Ask for persistent storage — on the first download, not before: a browser
 * that decides by "does this site look used" should be asked once there is
 * something worth keeping.
 */
export async function requestPersistence(): Promise<boolean> {
  try {
    if (await navigator.storage.persisted()) return true
    return await navigator.storage.persist()
  } catch {
    return false
  }
}
