import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createMissingStores,
  DB_VERSION,
  isPersistent,
  resetStores,
  resilientStore,
  STORE_NAMES,
} from '@/offline/store'

/**
 * A minimal hand-rolled IndexedDB: enough of `open`, upgrade, transactions,
 * requests and cursors for `store.ts`, and nothing else. Every callback fires
 * on a later macrotask, as the real thing's do.
 */

type Handler = (() => void) | null

class FakeRequest {
  result: unknown = undefined
  error: unknown = null
  onsuccess: Handler = null
  onerror: Handler = null
  onupgradeneeded: Handler = null
  onblocked: Handler = null
}

const later = (fn: () => void) => setTimeout(fn, 0)

class FakeDatabase {
  stores = new Map<string, Map<string, unknown>>()
  version: number
  onversionchange: Handler = null
  closed = false
  objectStoreNames = { contains: (name: string) => this.stores.has(name) }

  constructor(version: number) {
    this.version = version
  }

  createObjectStore(name: string): void {
    this.stores.set(name, new Map())
  }

  close(): void {
    this.closed = true
  }

  transaction(name: string) {
    const data = this.stores.get(name)
    if (data === undefined) throw new Error(`no store ${name}`)
    let pending = 0
    let done = false
    const tx = {
      oncomplete: null as Handler,
      onerror: null as Handler,
      onabort: null as Handler,
      error: null,
      objectStore: () => store,
    }
    const settle = () => {
      later(() => {
        if (pending === 0 && !done) {
          done = true
          tx.oncomplete?.()
        }
      })
    }
    const request = (run: () => unknown) => {
      const req = new FakeRequest()
      pending += 1
      later(() => {
        req.result = run()
        pending -= 1
        req.onsuccess?.()
        settle()
      })
      return req
    }
    const store = {
      put: (value: unknown, key: string) =>
        request(() => {
          data.set(key, value)
        }),
      get: (key: string) => request(() => data.get(key)),
      delete: (key: string) =>
        request(() => {
          data.delete(key)
        }),
      clear: () =>
        request(() => {
          data.clear()
        }),
      openCursor: () => {
        const req = new FakeRequest()
        const rows = [...data.entries()]
        let index = 0
        pending += 1
        const step = () => {
          later(() => {
            const row = rows[index]
            if (row === undefined) {
              req.result = null
              pending -= 1
              req.onsuccess?.()
              settle()
              return
            }
            req.result = {
              key: row[0],
              value: row[1],
              continue: () => {
                index += 1
                step()
              },
            }
            req.onsuccess?.()
          })
        }
        step()
        return req
      },
    }
    return tx
  }
}

/** A factory over one database, which can hold an upgrade until released. */
function fakeFactory(existing: FakeDatabase | null, options: { block?: boolean } = {}) {
  let db = existing
  let release: () => void = () => undefined
  const factory = {
    open: (_name: string, version: number) => {
      const request = new FakeRequest()
      const finish = () => {
        const needsUpgrade = db === null || db.version < version
        db ??= new FakeDatabase(version)
        request.result = db
        if (needsUpgrade) {
          request.onupgradeneeded?.()
          db.version = version
        }
        request.onsuccess?.()
      }
      if (options.block === true && existing !== null && existing.version < version) {
        later(() => request.onblocked?.())
        release = () => {
          later(finish)
        }
      } else {
        later(finish)
      }
      return request
    },
  }
  return {
    factory: factory as unknown as IDBFactory,
    release: () => {
      release()
    },
    db: () => db,
  }
}

// The shared setup has already opened the (absent, under jsdom) database.
beforeEach(() => {
  resetStores()
})

afterEach(() => {
  resetStores()
})

describe('the IndexedDB upgrade to v2 (FR-S9)', () => {
  it('creates the new stores and touches none that exist', () => {
    const outbox = new Map([['a', 1]])
    const db = new FakeDatabase(1)
    db.stores.set('outbox', outbox)

    createMissingStores(db)

    expect([...db.stores.keys()].sort()).toEqual([...STORE_NAMES].sort())
    expect(db.stores.get('outbox')).toBe(outbox)
  })

  it('opens a v1 database holding outbox records as v2 and keeps every record', async () => {
    const v1 = new FakeDatabase(1)
    v1.stores.set(
      'outbox',
      new Map<string, unknown>([
        ['r1', { id: 'r1', kind: 'position' }],
        ['r2', { id: 'r2', kind: 'completion' }],
      ]),
    )
    const { factory } = fakeFactory(v1)

    const entries = await resilientStore('outbox', factory).entries()

    expect(v1.version).toBe(DB_VERSION)
    expect(entries.map(([key]) => key).sort()).toEqual(['r1', 'r2'])
    expect(v1.stores.has('downloads')).toBe(true)
  })

  it('carries on in memory while an upgrade is blocked, then moves it all into the database', async () => {
    const v1 = new FakeDatabase(1)
    v1.stores.set('outbox', new Map<string, unknown>([['old', { id: 'old' }]]))
    const { factory, release } = fakeFactory(v1, { block: true })
    const store = resilientStore('outbox', factory)

    // Another tab holds v1 open: this one keeps working in memory.
    await store.put('new', { id: 'new' })
    expect(await store.get('new')).toEqual({ id: 'new' })
    expect(await isPersistent()).toBe(false)

    release()

    await vi.waitFor(() => {
      expect(v1.stores.get('outbox')?.get('new')).toEqual({ id: 'new' })
    })
    expect(v1.stores.get('outbox')?.get('old')).toEqual({ id: 'old' })
    // And the adopted connection yields to the next upgrade rather than blocking it.
    expect(v1.onversionchange).not.toBeNull()
    expect(await isPersistent()).toBe(true)
  })
})
