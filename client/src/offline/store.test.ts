import { afterEach, describe, expect, it } from 'vitest'
import { isPersistent, memoryStore, openStore, resetStores, resilientStore } from '@/offline/store'

afterEach(() => {
  resetStores()
})

describe('store', () => {
  it('memoryStore keeps what it is given', async () => {
    const store = memoryStore()
    await store.put('a', { n: 1 })
    expect(await store.get('a')).toEqual({ n: 1 })
    expect(await store.entries()).toEqual([['a', { n: 1 }]])
    await store.delete('a')
    expect(await store.get('a')).toBeUndefined()
  })

  it('falls back to memory with no IndexedDB, and says so', async () => {
    const store = resilientStore('outbox', null)
    await store.put('k', 'v')

    expect(await store.get('k')).toBe('v')
    expect(await store.entries()).toEqual([['k', 'v']])
    expect(await isPersistent()).toBe(false)
  })

  it('falls back to memory when opening the database throws', async () => {
    const throwing = {
      open: () => {
        throw new DOMException('denied', 'SecurityError')
      },
    } as unknown as IDBFactory
    const store = resilientStore('outbox', throwing)

    await store.put('k', 1)

    expect(await store.get('k')).toBe(1)
    expect(await isPersistent()).toBe(false)
  })

  it('never rejects, and the app store works in a browser without IndexedDB', async () => {
    const store = openStore('outbox')
    await expect(store.put('x', 1)).resolves.toBeUndefined()
    await expect(store.get('x')).resolves.toBe(1)
    await expect(store.clear()).resolves.toBeUndefined()
    expect(await store.entries()).toEqual([])
  })
})

describe('atomic operations (S3)', () => {
  afterEach(() => {
    resetStores()
  })

  for (const [name, make] of [
    ['memoryStore', () => memoryStore()],
    ['the memory fallback', () => resilientStore('outbox', null)],
  ] as const) {
    it(`${name}: deleteIf deletes only the value it was told about`, async () => {
      const store = make()
      await store.put('k', { id: 'new' })

      expect(await store.deleteIf('k', 'old')).toBe(false)
      expect(await store.get('k')).toEqual({ id: 'new' })
      expect(await store.deleteIf('k', 'new')).toBe(true)
      expect(await store.get('k')).toBeUndefined()
    })

    it(`${name}: update writes, deletes or leaves by the decision`, async () => {
      const store = make()
      await store.update<number>('n', (current) => (current ?? 0) + 1)
      await store.update<number>('n', (current) => (current ?? 0) + 1)
      expect(await store.get('n')).toBe(2)

      await store.update<number>('n', () => undefined)
      expect(await store.get('n')).toBe(2)
      await store.update<number>('n', () => null)
      expect(await store.get('n')).toBeUndefined()
    })
  }
})
