/**
 * What Arc remembers so that it opens and plays with no network (spec FR-S9,
 * architecture §5.4d). Ported from Audiosey's `offline/cache.ts`.
 *
 * The service worker caches the **app shell only** and never touches `/api/*`
 * or `/media/*` (FR-U1). So the data side of offline lives here, in IndexedDB,
 * written deliberately after a successful request:
 *
 * - `session` — the signed-in user, so that a request that fails for want of a
 *   network is not mistaken for a sign-out, and whose data this device holds.
 * - `payloads` — the last good Watch Now payload and every show page opened.
 * - `player` — the last position watched on this device, per (user, episode),
 *   which is what an offline resume seeks to and what an online one prefers
 *   when it is newer; and the position this device last knew the server to
 *   hold, which is how "newer" is told (2026-10-08).
 * - `covers` — a poster blob per show with a downloaded episode; the art lives
 *   on another origin, which is as unreachable offline as the API.
 *
 * Payloads belong to one account and are dropped on sign-out and when a
 * *different* account signs in. Positions carry the user id in their key and
 * stay. Covers are show data, not account data.
 */

import { isUnreachable } from '@/lib/api'
import type { User } from '@/lib/auth'
import { openStore, type KeyStore } from '@/offline/store'

const session = (): KeyStore => openStore('session')
const payloads = (): KeyStore => openStore('payloads')
const covers = (): KeyStore => openStore('covers')
const positions = (): KeyStore => openStore('player')

/* --- The signed-in user ---------------------------------------------------- */

const USER_KEY = 'user'
/** Whose payloads this device is holding. */
const OWNER_KEY = 'data_owner'

/**
 * Remember who is signed in. Unless the payloads already belong to this very
 * account, they are dropped — including when nobody owned them (after a
 * sign-out), since a request in flight at the sign-out may have written one.
 */
export async function rememberUser(user: User): Promise<void> {
  const owner = await session().get<number>(OWNER_KEY)
  if (owner !== user.id) await payloads().clear()
  await session().put(OWNER_KEY, user.id)
  await session().put(USER_KEY, user)
}

async function currentOwner(): Promise<number | null> {
  return (await session().get<number>(OWNER_KEY)) ?? null
}

export async function recallUser(): Promise<User | null> {
  return (await session().get<User>(USER_KEY)) ?? null
}

/** A real 401: forget the user. A failed request is not a sign-out. */
export async function forgetUser(): Promise<void> {
  await session().delete(USER_KEY)
}

/**
 * An explicit sign-out: the user and every remembered page go. Queued progress
 * (the outbox) and downloaded files do not — the first is the owner's rule
 * (FR-S8), the second is the user's to delete (FR-S9), and both are already
 * scoped to the account that made them.
 */
export async function forgetSession(): Promise<void> {
  await Promise.all([session().delete(USER_KEY), session().delete(OWNER_KEY), payloads().clear()])
}

/* --- A sign-out that has not reached the server yet ------------------------------- */

const LOGOUT_PENDING_KEY = 'logout_pending'

/**
 * A sign-out made with no network leaves the session cookie valid on the
 * server, so the next online launch would sign the same person back in — on
 * a shared iPad, the wrong person. The flag is durable until the server has
 * been told; while it is set the app treats itself as signed out.
 */
export async function markLogoutPending(): Promise<void> {
  await session().put(LOGOUT_PENDING_KEY, true)
}

export async function isLogoutPending(): Promise<boolean> {
  return (await session().get<boolean>(LOGOUT_PENDING_KEY)) === true
}

export async function clearLogoutPending(): Promise<void> {
  await session().delete(LOGOUT_PENDING_KEY)
}

/* --- Page payloads ----------------------------------------------------------- */

export const HOME_PAYLOAD = 'home'

export function animePayloadKey(animeId: number): string {
  return `anime:${String(animeId)}`
}

interface StoredPayload<T> {
  at: string
  /** The account whose request produced it; recalled only for that account. */
  owner: number | null
  value: T
}

/** `owner` is whose request this was — taken before the request, not after. */
export async function rememberPayload(
  key: string,
  value: unknown,
  owner?: number | null,
): Promise<void> {
  const stamp = owner === undefined ? await currentOwner() : owner
  await payloads().put(key, { at: new Date().toISOString(), owner: stamp, value })
}

/** The last good copy for the account signed in now, or `undefined`. */
export async function recallPayload<T>(key: string): Promise<T | undefined> {
  const found = await payloads().get<StoredPayload<T>>(key)
  if (found === undefined) return undefined
  if ((found.owner ?? null) !== (await currentOwner())) return undefined
  return found.value
}

/**
 * Network first; the answer is remembered. When the server cannot be reached
 * the last good copy stands in, and with none the original error is thrown so
 * the page says what it always said.
 */
export async function withRemembered<T>(key: string, load: () => Promise<T>): Promise<T> {
  // Whose request this is, fixed before it leaves: an answer that lands after
  // a sign-out is stamped with the account that asked, and never shown to the
  // next one.
  const owner = await currentOwner()
  try {
    const value = await load()
    void rememberPayload(key, value, owner)
    return value
  } catch (error) {
    if (isUnreachable(error)) {
      const saved = await recallPayload<T>(key)
      if (saved !== undefined) return saved
    }
    throw error
  }
}

/* --- The last position on this device ----------------------------------------- */

/**
 * The last position watched on this device, per (user, episode), whatever the
 * source — the stream or the device's copy — and whether or not the network
 * was there. It is what an offline resume seeks to, and online it is weighed
 * against the server's (FR-S2, FR-S9, 2026-10-08: `newerResume` in
 * `lib/playback.ts`).
 */
export interface LocalPosition {
  position_s: number
  duration_s: number
  /** When it was written. The record's first field for this; always written. */
  at: string
  /**
   * The same moment under the name the resume rule reads (2026-10-08).
   * Optional: records written before then carry `at` only, which
   * {@link positionWrittenAt} falls back to.
   */
  updated_at?: string
}

/** When a local position was written, in ms since the epoch; 0 when unreadable. */
export function positionWrittenAt(local: LocalPosition): number {
  const parsed = Date.parse(local.updated_at ?? local.at)
  return Number.isFinite(parsed) ? parsed : 0
}

function positionKey(userId: number, episodeId: number): string {
  return `pos:${String(userId)}:${String(episodeId)}`
}

export async function rememberPosition(
  userId: number,
  episodeId: number,
  position: number,
  duration: number,
  now: Date = new Date(),
): Promise<void> {
  if (!Number.isFinite(position) || position < 0) return
  const stamp = now.toISOString()
  await positions().put(positionKey(userId, episodeId), {
    position_s: position,
    duration_s: Number.isFinite(duration) ? duration : 0,
    at: stamp,
    updated_at: stamp,
  } satisfies LocalPosition)
}

export async function recallPosition(
  userId: number,
  episodeId: number,
): Promise<LocalPosition | null> {
  return (await positions().get<LocalPosition>(positionKey(userId, episodeId))) ?? null
}

/**
 * What this device last knew the server to hold for the episode, and when it
 * learned it (2026-10-08). Learned from three places only: the play answer it
 * opened with (when the server's position was the one used), a progress report
 * the server accepted, and an outbox item the server applied. `position_s` is
 * null when the play answer had nothing to resume.
 *
 * The play answer carries no timestamp, so this is how the client tells "the
 * server's position is still the one this device last saw, and the device has
 * watched since" (the device's is newer) from "something else moved it"
 * (another device: the server's is newer).
 */
export interface ServerPosition {
  position_s: number | null
  duration_s: number
  at: string
}

function serverKey(userId: number, episodeId: number): string {
  return `srv:${String(userId)}:${String(episodeId)}`
}

export async function noteServerPosition(
  userId: number,
  episodeId: number,
  position: number | null,
  duration: number,
  now: Date = new Date(),
): Promise<void> {
  if (position !== null && (!Number.isFinite(position) || position < 0)) return
  await positions().put(serverKey(userId, episodeId), {
    position_s: position,
    duration_s: Number.isFinite(duration) && duration > 0 ? duration : 0,
    at: now.toISOString(),
  } satisfies ServerPosition)
}

export async function recallServerPosition(
  userId: number,
  episodeId: number,
): Promise<ServerPosition | null> {
  return (await positions().get<ServerPosition>(serverKey(userId, episodeId))) ?? null
}

/* --- Posters ---------------------------------------------------------------- */

/** Per account, like everything else a download remembers. */
function coverKey(userId: number, animeId: number): string {
  return `u${String(userId)}:anime:${String(animeId)}`
}

const coverUrls = new Map<string, string>()

/**
 * Fetch a show's poster once and keep the bytes. Quiet about every failure: a
 * poster host that does not allow a cross-origin read, or no network, leaves a
 * placeholder — never an error.
 */
export async function rememberCover(
  userId: number,
  animeId: number,
  url: string | null,
  load: (url: string) => Promise<Response> = (target) => fetch(target, { mode: 'cors' }),
): Promise<void> {
  if (url === null || url === '') return
  if ((await covers().get(coverKey(userId, animeId))) !== undefined) return
  try {
    const response = await load(url)
    if (!response.ok) return
    await covers().put(coverKey(userId, animeId), await response.blob())
  } catch {
    // Unreadable from here. The Downloads page draws the placeholder.
  }
}

/** A blob URL for a remembered poster, memoised so re-renders mint nothing new. */
export async function coverUrl(userId: number, animeId: number): Promise<string | null> {
  const key = coverKey(userId, animeId)
  const existing = coverUrls.get(key)
  if (existing !== undefined) return existing
  const blob = await covers().get<Blob>(key)
  if (!(blob instanceof Blob)) return null
  const url = URL.createObjectURL(blob)
  coverUrls.set(key, url)
  return url
}

export async function forgetCover(userId: number, animeId: number): Promise<void> {
  const key = coverKey(userId, animeId)
  const url = coverUrls.get(key)
  if (url !== undefined) {
    URL.revokeObjectURL(url)
    coverUrls.delete(key)
  }
  await covers().delete(key)
}
