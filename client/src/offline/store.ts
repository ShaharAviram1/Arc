/**
 * A tiny key/value store over IndexedDB, written by hand (no `idb` package).
 *
 * Everything the device has to remember between launches goes through this.
 * Today that is the progress outbox (FR-S8); the next M18 tasks add episode
 * downloads, offline page payloads, cover blobs and the signed-in session on
 * top of it. It is generic on purpose: a consumer names a store and gets a
 * {@link KeyStore}; it never sees IndexedDB.
 *
 * Four decisions are worth stating.
 *
 * **It is an interface, not a module of functions.** Every consumer takes a
 * `KeyStore`, so a test hands it {@link memoryStore} and needs no IndexedDB
 * shim, and the app hands it {@link openStore}.
 *
 * **Nothing here ever rejects.** A read that fails answers `undefined`, a
 * write that fails answers nothing. A private window, storage disabled, a full
 * quota: the app keeps working in all of them, and an unhandled rejection in
 * the middle of an episode is the one thing that must not happen. The last
 * failure is kept on {@link lastError}.
 *
 * **But a write is never silently lost.** When IndexedDB cannot be opened at
 * all (Firefox private windows, some embedded webviews) every store falls back
 * to memory; when a single write fails (quota), that value is kept in a
 * per-store memory overlay that reads consult first. Either way the data lives
 * for as long as the page does, and {@link isPersistent} turns false so the UI
 * can say that it will not survive a reload.
 *
 * **One database, one object store per concern.** Object stores can only be
 * created inside an upgrade, so they are declared in {@link STORE_NAMES}. To
 * add one (`downloads`, `payloads`, `covers`, `session` are the planned
 * ones): append the name and bump {@link DB_VERSION}. The upgrade creates any
 * store that is missing and never touches one that exists, so the outbox's
 * queued records survive every future upgrade. An open tab holding the old
 * version is asked to close its connection (`onversionchange`), and reopens
 * on its next operation.
 */

export const DB_NAME = 'arc'
export const DB_VERSION = 2

/** Every object store. Append, then bump {@link DB_VERSION}; never rename or remove. */
export const STORE_NAMES = [
  /** Playback progress recorded while offline, waiting to be synced (FR-S8). */
  'outbox',
  /** v2 (FR-S9): one record per (user, episode) kept on this device — state, bytes, ETag, what to show. */
  'downloads',
  /** v2 (FR-S9): the last position watched on this device, per (user, episode), for an offline resume. */
  'player',
  /** v2 (FR-S9): the last good Watch Now and show-page payloads, for a launch with no network. */
  'payloads',
  /** v2 (FR-S9): poster blobs for shows with a downloaded episode (the art is on another origin). */
  'covers',
  /** v2 (FR-S9): the signed-in user, so a launch with no network is not a sign-out. */
  'session',
] as const

export type StoreName = (typeof STORE_NAMES)[number]

/**
 * What {@link KeyStore.update}'s `decide` answers: a value to write, `null` to
 * delete the key, or `undefined` to leave it exactly as it is.
 */
export type Decision<T> = T | null | undefined

export interface KeyStore {
  get<T>(key: string): Promise<T | undefined>
  put(key: string, value: unknown): Promise<void>
  delete(key: string): Promise<void>
  /** Every entry, in no particular order, read in one transaction. */
  entries<T>(): Promise<[string, T][]>
  clear(): Promise<void>
  /**
   * Read, decide and write **in one transaction**, so nothing can land between
   * the read and the write. `decide` must be synchronous (an IndexedDB
   * transaction closes at the first `await`). This is the store's
   * compare-and-set: newest-wins writes and "delete only if unchanged" are
   * both made of it.
   */
  update<T>(key: string, decide: (current: T | undefined) => Decision<T>): Promise<void>
  /**
   * Delete `key` only if its value still has `id === expectedId`, atomically.
   * Answers whether it deleted. For stores whose values carry an `id`.
   */
  deleteIf(key: string, expectedId: string): Promise<boolean>
}

function hasId(value: unknown, id: string): boolean {
  return typeof value === 'object' && value !== null && (value as { id?: unknown }).id === id
}

/** The last failure any store saw, for callers that want to say "not saved". */
export let lastError: unknown = null

/** Whether writes are reaching IndexedDB. False once anything fell back to memory. */
let persistent = true
const persistenceListeners = new Set<() => void>()

function degrade(error: unknown): void {
  lastError = error
  if (!persistent) return
  persistent = false
  for (const listener of persistenceListeners) listener()
}

/** An in-memory store: the fallback when IndexedDB is missing, and what tests use. */
export function memoryStore(initial?: Iterable<[string, unknown]>): KeyStore {
  const map = new Map<string, unknown>(initial)
  return {
    get<T>(key: string) {
      return Promise.resolve(map.get(key) as T | undefined)
    },
    put(key: string, value: unknown) {
      map.set(key, value)
      return Promise.resolve()
    },
    delete(key: string) {
      map.delete(key)
      return Promise.resolve()
    },
    entries<T>() {
      return Promise.resolve([...map.entries()] as [string, T][])
    },
    clear() {
      map.clear()
      return Promise.resolve()
    },
    update<T>(key: string, decide: (current: T | undefined) => Decision<T>) {
      // Synchronous from read to write: nothing can interleave.
      const next = decide(map.get(key) as T | undefined)
      if (next === null) map.delete(key)
      else if (next !== undefined) map.set(key, next)
      return Promise.resolve()
    },
    deleteIf(key: string, expectedId: string) {
      if (!hasId(map.get(key), expectedId)) return Promise.resolve(false)
      map.delete(key)
      return Promise.resolve(true)
    },
  }
}

let database: Promise<IDBDatabase | null> | null = null

/**
 * Stores waiting for a database that arrived late (a blocked upgrade that
 * another tab finally let through). Each one moves what it kept in memory
 * meanwhile into the real database.
 */
const adopters = new Set<(db: IDBDatabase) => Promise<void>>()

/** The part of an upgrade that matters: make each missing store, touch none that exist. */
export function createMissingStores(db: {
  objectStoreNames: { contains(name: string): boolean }
  createObjectStore(name: string): unknown
}): void {
  for (const name of STORE_NAMES) {
    if (!db.objectStoreNames.contains(name)) db.createObjectStore(name)
  }
}

/** Close this connection when another tab wants a newer version, and reopen later. */
function yieldToNewerVersions(db: IDBDatabase): void {
  db.onversionchange = () => {
    db.close()
    database = null
  }
}

function openDatabase(factory: IDBFactory | null): Promise<IDBDatabase | null> {
  if (database !== null) return database
  let blocked = false
  database = new Promise<IDBDatabase | null>((resolve) => {
    if (factory === null) {
      degrade(new Error('IndexedDB is not available'))
      resolve(null)
      return
    }
    let request: IDBOpenDBRequest
    try {
      request = factory.open(DB_NAME, DB_VERSION)
    } catch (error) {
      degrade(error)
      resolve(null)
      return
    }
    request.onupgradeneeded = () => {
      createMissingStores(request.result)
    }
    request.onsuccess = () => {
      const db = request.result
      // Another tab is upgrading: let it, and reopen on the next operation.
      yieldToNewerVersions(db)
      if (!blocked) {
        resolve(db)
        return
      }
      // The upgrade was blocked and this tab carried on in memory; the other
      // tab has let go. Adopt the real database and move into it everything
      // that was kept in memory meanwhile — the outbox's records included.
      database = Promise.resolve(db)
      persistent = true
      for (const adopt of adopters) void adopt(db)
    }
    request.onerror = () => {
      degrade(request.error)
      resolve(null)
    }
    // Another tab holds an old version open and will not let go. Memory keeps
    // this one working rather than hanging on a promise that never settles —
    // but the request stays open, and `onsuccess` above adopts the database
    // the moment the other tab closes its connection.
    request.onblocked = () => {
      blocked = true
      degrade(new Error('IndexedDB upgrade blocked by another tab'))
      resolve(null)
    }
  })
  return database
}

/** One read. Resolves `fallback` on any failure. */
function read<T>(
  db: IDBDatabase,
  name: StoreName,
  body: (store: IDBObjectStore) => IDBRequest,
  fallback: T,
): Promise<T> {
  return new Promise<T>((resolve) => {
    try {
      const request = body(db.transaction(name, 'readonly').objectStore(name))
      request.onsuccess = () => {
        resolve(request.result as T)
      }
      request.onerror = () => {
        lastError = request.error
        resolve(fallback)
      }
    } catch (error) {
      lastError = error
      resolve(fallback)
    }
  })
}

/** One write. Resolves whether it was committed (the transaction completed). */
function write(
  db: IDBDatabase,
  name: StoreName,
  body: (store: IDBObjectStore) => void,
): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    try {
      const transaction = db.transaction(name, 'readwrite')
      transaction.oncomplete = () => {
        resolve(true)
      }
      transaction.onerror = () => {
        lastError = transaction.error
        resolve(false)
      }
      transaction.onabort = () => {
        lastError = transaction.error
        resolve(false)
      }
      body(transaction.objectStore(name))
    } catch (error) {
      lastError = error
      resolve(false)
    }
  })
}

/**
 * Read one key and write the decision in the same readwrite transaction.
 * Resolves the decision when the transaction committed, `false` when it did
 * not (the caller then keeps the value in memory).
 */
function atomic<T>(
  db: IDBDatabase,
  name: StoreName,
  key: string,
  decide: (current: T | undefined) => Decision<T>,
): Promise<{ committed: boolean; decision: Decision<T> }> {
  return new Promise((resolve) => {
    let decision: Decision<T> = undefined
    try {
      const transaction = db.transaction(name, 'readwrite')
      const store = transaction.objectStore(name)
      transaction.oncomplete = () => {
        resolve({ committed: true, decision })
      }
      transaction.onerror = () => {
        lastError = transaction.error
        resolve({ committed: false, decision })
      }
      transaction.onabort = () => {
        lastError = transaction.error
        resolve({ committed: false, decision })
      }
      const request = store.get(key)
      request.onsuccess = () => {
        decision = decide(request.result as T | undefined)
        if (decision === null) store.delete(key)
        else if (decision !== undefined) store.put(decision, key)
      }
    } catch (error) {
      lastError = error
      resolve({ committed: false, decision })
    }
  })
}

/** Every key and value, from one cursor in one transaction. */
function readAll(db: IDBDatabase, name: StoreName): Promise<[string, unknown][]> {
  return new Promise((resolve) => {
    const out: [string, unknown][] = []
    try {
      const request = db.transaction(name, 'readonly').objectStore(name).openCursor()
      request.onsuccess = () => {
        const cursor = request.result
        if (cursor === null) {
          resolve(out)
          return
        }
        // Every key this store is given is a string.
        out.push([cursor.key as string, cursor.value])
        cursor.continue()
      }
      request.onerror = () => {
        lastError = request.error
        resolve(out)
      }
    } catch (error) {
      lastError = error
      resolve(out)
    }
  })
}

function defaultFactory(): IDBFactory | null {
  return typeof indexedDB === 'undefined' ? null : indexedDB
}

/**
 * A store backed by IndexedDB, with a memory overlay for whatever IndexedDB
 * would not take. `factory` is injectable for tests; `null` means "there is
 * no IndexedDB" (the private-window case).
 */
export function resilientStore(name: StoreName, factory: IDBFactory | null): KeyStore {
  const overlay = new Map<string, unknown>()
  /** Deletes made while there was no database, replayed if one turns up. */
  const tombstones = new Set<string>()
  let clearedWithoutDatabase = false
  const db = () => openDatabase(factory)

  adopters.add(async (handle) => {
    if (clearedWithoutDatabase) {
      clearedWithoutDatabase = false
      await write(handle, name, (store) => store.clear())
    }
    for (const key of [...tombstones]) {
      if (await write(handle, name, (store) => store.delete(key))) tombstones.delete(key)
    }
    for (const [key, value] of [...overlay]) {
      const committed = await write(handle, name, (store) => store.put(value, key))
      // Only if nothing newer was kept meanwhile.
      if (committed && overlay.get(key) === value) overlay.delete(key)
    }
  })

  return {
    async get<T>(key: string) {
      if (overlay.has(key)) return overlay.get(key) as T
      const handle = await db()
      if (handle === null) return undefined
      return read<T | undefined>(handle, name, (store) => store.get(key), undefined)
    },
    async put(key: string, value: unknown) {
      const handle = await db()
      if (handle !== null && (await write(handle, name, (store) => store.put(value, key)))) {
        overlay.delete(key)
        return
      }
      tombstones.delete(key)
      overlay.set(key, value)
      degrade(lastError ?? new Error(`could not write to ${name}`))
    },
    async delete(key: string) {
      overlay.delete(key)
      const handle = await db()
      if (handle !== null) await write(handle, name, (store) => store.delete(key))
      else tombstones.add(key)
    },
    async entries<T>() {
      const handle = await db()
      const merged = new Map<string, unknown>(handle === null ? [] : await readAll(handle, name))
      for (const [key, value] of overlay) merged.set(key, value)
      return [...merged.entries()] as [string, T][]
    },
    async update<T>(key: string, decide: (current: T | undefined) => Decision<T>) {
      if (overlay.has(key)) {
        // The value lives in memory already; decide there, synchronously.
        const next = decide(overlay.get(key) as T)
        if (next === null) overlay.delete(key)
        else if (next !== undefined) overlay.set(key, next)
        return
      }
      const handle = await db()
      if (handle === null) {
        const next = decide(undefined)
        if (next !== null && next !== undefined) overlay.set(key, next)
        return
      }
      const { committed, decision } = await atomic<T>(handle, name, key, decide)
      if (!committed && decision !== null && decision !== undefined) {
        overlay.set(key, decision)
        degrade(lastError ?? new Error(`could not write to ${name}`))
      }
    },
    async deleteIf(key: string, expectedId: string) {
      if (overlay.has(key)) {
        if (!hasId(overlay.get(key), expectedId)) return false
        overlay.delete(key)
        return true
      }
      const handle = await db()
      if (handle === null) return false
      const { committed, decision } = await atomic<unknown>(handle, name, key, (current) =>
        hasId(current, expectedId) ? null : undefined,
      )
      return committed && decision === null
    },
    async clear() {
      overlay.clear()
      tombstones.clear()
      const handle = await db()
      if (handle !== null) await write(handle, name, (store) => store.clear())
      else clearedWithoutDatabase = true
    },
  }
}

const opened = new Map<StoreName, KeyStore>()

/** The app's store for one concern: IndexedDB when it works, memory when it does not. */
export function openStore(name: StoreName): KeyStore {
  const existing = opened.get(name)
  if (existing !== undefined) return existing
  const store = resilientStore(name, defaultFactory())
  opened.set(name, store)
  return store
}

/**
 * Whether what is stored will survive a reload. Opens the database if nothing
 * has yet, so the answer is about this device rather than about "not tried".
 */
export async function isPersistent(): Promise<boolean> {
  await openDatabase(defaultFactory())
  return persistent
}

/** Called whenever {@link isPersistent} turns false. Returns an unsubscribe. */
export function onPersistenceLost(listener: () => void): () => void {
  persistenceListeners.add(listener)
  return () => {
    persistenceListeners.delete(listener)
  }
}

/** Drop every cached handle and flag. Tests only. */
export function resetStores(): void {
  opened.clear()
  adopters.clear()
  database = null
  lastError = null
  persistent = true
  persistenceListeners.clear()
}
