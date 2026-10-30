import { describe, expect, it } from 'vitest'
import { EVENT_LOG_KEY, EVENT_LOG_MAX, EventLog, type DeviceEvent } from '@/offline/eventLog'
import { memoryStore, type KeyStore } from '@/offline/store'

/** The download manager's diary (M20.4 E): a ring buffer in IndexedDB `meta`. */

describe('EventLog', () => {
  it('keeps entries in order, with the time, the episode and the detail', async () => {
    let now = Date.parse('2026-10-30T03:30:00Z')
    const log = new EventLog(memoryStore(), () => now)

    log.note('state', 15, 'downloading → downloaded')
    now += 1000
    log.note('holdings')

    expect(await log.entries()).toEqual([
      {
        at: '2026-10-30T03:30:00.000Z',
        kind: 'state',
        episode_id: 15,
        detail: 'downloading → downloaded',
      },
      { at: '2026-10-30T03:30:01.000Z', kind: 'holdings' },
    ])
  })

  it(`keeps only the newest ${String(EVENT_LOG_MAX)}`, async () => {
    const store = memoryStore()
    const log = new EventLog(store)
    for (let index = 0; index < EVENT_LOG_MAX + 25; index += 1) log.note('n', index)

    const entries = await log.entries()
    expect(entries).toHaveLength(EVENT_LOG_MAX)
    expect(entries[0]?.episode_id).toBe(25)
    expect(entries.at(-1)?.episode_id).toBe(EVENT_LOG_MAX + 24)
    expect(await store.get<DeviceEvent[]>(EVENT_LOG_KEY)).toHaveLength(EVENT_LOG_MAX)
  })

  it('cuts a long detail short', async () => {
    const log = new EventLog(memoryStore())
    log.note('failed', 1, 'x'.repeat(2000))
    expect((await log.entries())[0]?.detail).toHaveLength(300)
  })

  it('survives a store that fails, and reads back across instances', async () => {
    const broken: KeyStore = {
      ...memoryStore(),
      update: () => Promise.reject(new Error('quota')),
      get: () => Promise.reject(new Error('gone')),
    }
    const log = new EventLog(broken)
    log.note('state', 1)
    expect(await log.entries()).toEqual([])

    const store = memoryStore()
    new EventLog(store).note('reconcile', 3)
    await new EventLog(store).entries()
    expect(await new EventLog(store).entries()).toHaveLength(1)
  })
})
