import { describe, expect, it, vi } from 'vitest'
import {
  keyOf,
  LOCK_NAME,
  MAX_BATCH,
  Outbox,
  STUCK_AFTER_MS,
  STUCK_ATTEMPTS,
  type ItemStatus,
  type Locks,
  type OutboxRecord,
  type Send,
  type SyncBody,
  type SyncResponse,
} from '@/offline/outbox'
import { memoryStore, type KeyStore } from '@/offline/store'
import { recallServerPosition } from '@/offline/cache'
import { notePositionApplied } from '@/offline/useOutbox'

const ALICE = 1
const BOB = 2
const EP = 501
const DURATION = 1420

/** A clock that moves one second per reading, so every record has its own moment. */
function clock(start = Date.UTC(2026, 9, 4, 12, 0, 0)) {
  let at = start
  return () => {
    at += 1000
    return new Date(at)
  }
}

function ids() {
  let next = 0
  return () => {
    next += 1
    return `id-${String(next)}`
  }
}

/** A server that answers every item with `status`, or per item by kind. */
function answering(
  status: (item: SyncBody['items'][number]) => ItemStatus = () => 'applied',
  reason = 'episode no longer exists',
) {
  return vi.fn<Send>((body) =>
    Promise.resolve<SyncResponse>({
      results: body.items.map((item) => {
        const answer = status(item)
        return {
          client_id: item.client_id,
          status: answer,
          reason: answer === 'rejected' || answer === 'retry' ? reason : null,
        }
      }),
    }),
  )
}

function make(send: Send = answering(), store: KeyStore = memoryStore(), now = clock()) {
  const box = new Outbox({
    store,
    send,
    now,
    newId: ids(),
    persistent: () => Promise.resolve(true),
    locks: null,
  })
  box.setOwner(ALICE)
  return { box, store }
}

const kinds = (records: OutboxRecord[]) => records.map((record) => record.kind)

describe('Outbox', () => {
  it('coalesces positions per episode but never coalesces a completion away', async () => {
    const { box } = make()

    await box.recordPosition(ALICE, EP, 600, DURATION)
    await box.recordPosition(ALICE, EP, 1300, DURATION) // crosses 90 %
    await box.recordPosition(ALICE, EP, 60, DURATION) // the viewer rewinds

    const records = await box.all()
    expect(kinds(records)).toEqual(['completion', 'position'])
    expect(records[0]).toMatchObject({ kind: 'completion', position_s: 1300 })
    expect(records[1]).toMatchObject({ kind: 'position', position_s: 60 })
  })

  it('creates one completion per crossing, and a new one after an un-mark', async () => {
    const { box } = make()

    await box.recordPosition(ALICE, EP, 1300, DURATION)
    await box.recordPosition(ALICE, EP, 1350, DURATION)
    expect(kinds(await box.all()).filter((kind) => kind === 'completion')).toHaveLength(1)

    await box.recordUnmark(ALICE, EP)
    await box.recordPosition(ALICE, EP, 1360, DURATION)
    // The sample and the completion it created share a moment; `seq` keeps
    // them in the order they were made.
    expect(kinds(await box.all())).toEqual(['completion', 'unmark', 'position', 'completion'])
  })

  it('replays records in the order they happened, across kinds', async () => {
    const send = answering()
    const { box } = make(send)

    await box.recordCompletion(ALICE, EP)
    await box.recordUnmark(ALICE, EP)
    await box.recordPosition(ALICE, EP, 100, DURATION)
    await box.recordCompletion(ALICE, EP + 1)

    await box.flush()

    const body = send.mock.calls[0]?.[0]
    expect(body?.user_id).toBe(ALICE)
    expect(body?.items.map((item) => [item.kind, item.episode_id])).toEqual([
      ['completion', EP],
      ['unmark', EP],
      ['position', EP],
      ['completion', EP + 1],
    ])
  })

  it('keeps every record when the request fails, only counting the attempt', async () => {
    const send = vi.fn<Send>(() => Promise.reject(new TypeError('Failed to fetch')))
    const { box } = make(send)
    await box.recordPosition(ALICE, EP, 1300, DURATION)
    const before = await box.all()

    const outcome = await box.flush()

    expect(outcome.ok).toBe(false)
    expect(await box.all()).toEqual(
      before.map((record) => ({ ...record, attempts: 1, last_error: 'Failed to fetch' })),
    )
  })

  it('removes applied and stale records on the server’s word', async () => {
    const { box } = make(answering((item) => (item.kind === 'position' ? 'stale' : 'applied')))
    await box.recordPosition(ALICE, EP, 1300, DURATION)

    const outcome = await box.flush()

    expect(outcome).toMatchObject({ ok: true, sent: 2, removed: 2, rejected: 0 })
    expect(await box.all()).toEqual([])
  })

  it('keeps a record the server did not mention', async () => {
    const send = vi.fn<Send>(() => Promise.resolve({ results: [] }))
    const { box } = make(send)
    await box.recordPosition(ALICE, EP, 100, DURATION)

    await box.flush()

    expect(await box.all()).toHaveLength(1)
  })

  it('keeps a sample that was superseded while the flush was in flight', async () => {
    let release: (value: SyncResponse) => void = () => undefined
    const send = vi.fn<Send>(
      (body) =>
        new Promise<SyncResponse>((resolve) => {
          release = () => {
            resolve({
              results: body.items.map((item) => ({
                client_id: item.client_id,
                status: 'applied',
                reason: null,
              })),
            })
          }
        }),
    )
    const { box } = make(send)
    await box.recordPosition(ALICE, EP, 100, DURATION)

    const flushing = box.flush()
    await vi.waitFor(() => {
      expect(send).toHaveBeenCalledTimes(1)
    })
    await box.recordPosition(ALICE, EP, 200, DURATION)
    release({ results: [] })
    await flushing

    const records = await box.all()
    expect(records).toHaveLength(1)
    expect(records[0]?.position_s).toBe(200)
  })

  it('keeps a rejected record, marks it, surfaces it, and deletes it only on dismiss', async () => {
    const { box, store } = make(answering(() => 'rejected'))
    await box.recordPosition(ALICE, EP, 100, DURATION)

    const outcome = await box.flush()

    expect(outcome.rejected).toBe(1)
    const [kept] = await box.all()
    expect(kept?.problem).toBe('episode no longer exists')
    expect(box.getSnapshot().problems).toHaveLength(1)
    expect(box.getSnapshot().waiting).toBe(0)

    // A new sample for the same episode does not coalesce over the problem.
    await box.recordPosition(ALICE, EP, 300, DURATION)
    expect(await box.all()).toHaveLength(2)

    // And a rejected record is never sent again.
    const send = answering()
    const again = new Outbox({ store, send, persistent: () => Promise.resolve(true), locks: null })
    again.setOwner(ALICE)
    await again.flush()
    expect(send.mock.calls[0]?.[0].items).toHaveLength(1)

    if (kept === undefined) throw new Error('no record')
    await box.dismiss(kept.id)
    expect(await store.get(keyOf(kept))).toBeUndefined()
    expect(box.getSnapshot().problems).toEqual([])
  })

  it('never sends one account’s records as another’s, and wipes nothing on a switch', async () => {
    const send = answering()
    const { box } = make(send)
    await box.recordPosition(ALICE, EP, 100, DURATION)

    box.setOwner(BOB)
    await box.recordPosition(BOB, EP, 50, DURATION)
    await box.flush()

    expect(send).toHaveBeenCalledTimes(1)
    expect(send.mock.calls[0]?.[0]).toMatchObject({ user_id: BOB })
    expect(send.mock.calls[0]?.[0].items.map((item) => item.position_s)).toEqual([50])
    const left = await box.all()
    expect(left.map((record) => record.user_id)).toEqual([ALICE])
    expect(box.getSnapshot().foreign).toBe(1)

    box.setOwner(ALICE)
    await box.flush()
    expect(send.mock.calls[1]?.[0]).toMatchObject({ user_id: ALICE })
    expect(await box.all()).toEqual([])
  })

  it('sends nothing while nobody is signed in', async () => {
    const send = answering()
    const { box } = make(send)
    await box.recordPosition(ALICE, EP, 100, DURATION)
    box.setOwner(null)

    await box.flush()

    expect(send).not.toHaveBeenCalled()
    expect(box.recordingAs).toBe(ALICE)
  })

  it('sends a long queue in batches of the server’s cap', async () => {
    const send = answering()
    const { box } = make(send)
    for (let episode = 1; episode <= MAX_BATCH + 5; episode += 1) {
      await box.recordPosition(ALICE, episode, 10, DURATION)
    }

    await box.flush()

    expect(send.mock.calls.map((call) => call[0].items.length)).toEqual([MAX_BATCH, 5])
    expect(await box.all()).toEqual([])
  })

  it('runs one flush at a time', async () => {
    const send = answering()
    const { box } = make(send)
    await box.recordPosition(ALICE, EP, 100, DURATION)

    await Promise.all([box.flush(), box.flush()])

    expect(send).toHaveBeenCalledTimes(1)
  })

  it('a sample of unknown fate never mints a completion (B1)', async () => {
    const { box } = make()
    await box.recordPosition(ALICE, EP, 1300, DURATION, { completion: false })

    expect(kinds(await box.all())).toEqual(['position'])
  })

  it('mints no completion for an episode the server already said is completed', async () => {
    const { box } = make()
    box.noteCompleted(ALICE, EP, true)
    await box.recordPosition(ALICE, EP, 1300, DURATION)
    expect(kinds(await box.all())).toEqual(['position'])

    // Un-marking forgets it: a later crossing is a new completion.
    await box.recordUnmark(ALICE, EP)
    await box.recordPosition(ALICE, EP, 1310, DURATION)
    expect(kinds(await box.all())).toEqual(['unmark', 'position', 'completion'])
  })

  it('orders by the device’s own sequence, not its clock (S6)', async () => {
    // The clock steps backwards an hour between the mark and the un-mark.
    const times = [Date.UTC(2026, 9, 4, 12), Date.UTC(2026, 9, 4, 11)]
    let reading = 0
    const now = () => new Date(times[Math.min(reading++, times.length - 1)] ?? 0)
    const send = answering()
    const { box } = make(send, memoryStore(), now)

    await box.recordCompletion(ALICE, EP)
    await box.recordUnmark(ALICE, EP)
    await box.flush()

    expect(send.mock.calls[0]?.[0].items.map((item) => item.kind)).toEqual(['completion', 'unmark'])
  })

  it('hands out distinct sequence numbers to concurrent first calls (S6)', async () => {
    const { box } = make()
    await Promise.all([
      box.recordCompletion(ALICE, EP),
      box.recordUnmark(ALICE, EP),
      box.recordCompletion(ALICE, EP + 1),
    ])

    const seqs = (await box.all()).map((record) => record.seq)
    expect(new Set(seqs).size).toBe(3)
  })

  it('never deletes a sample that landed between settle’s read and its delete (S3)', async () => {
    const inner = memoryStore()
    let box: Outbox | null = null
    let interleaved = false
    // A store where the moment anything reads the pending position, a newer
    // sample is written — the gap a get-then-delete would have.
    const store: KeyStore = {
      ...inner,
      get: async <T>(key: string) => {
        const value = await inner.get<T>(key)
        if (!interleaved && key.startsWith('position:') && box !== null) {
          interleaved = true
          await box.recordPosition(ALICE, EP, 200, DURATION)
        }
        return value
      },
      deleteIf: async (key, id) => {
        if (!interleaved && box !== null) {
          interleaved = true
          await box.recordPosition(ALICE, EP, 200, DURATION)
        }
        return inner.deleteIf(key, id)
      },
    }
    box = make(answering(), store).box
    await box.recordPosition(ALICE, EP, 100, DURATION)

    await box.flush()

    const records = await box.all()
    expect(records).toHaveLength(1)
    expect(records[0]?.position_s).toBe(200)
  })

  it('keeps a record answered retry pending, and counts the attempt (S1)', async () => {
    const send = answering(() => 'retry', 'the server could not apply it just now')
    const { box } = make(send)
    await box.recordCompletion(ALICE, EP)

    await box.flush()

    const [record] = await box.all()
    expect(record?.problem).toBeUndefined()
    expect(record).toMatchObject({
      attempts: 1,
      last_error: 'the server could not apply it just now',
    })
    expect(box.getSnapshot().waiting).toBe(1)
  })

  it('shows records that keep failing, without deleting them (S2)', async () => {
    let at = Date.UTC(2026, 9, 4, 12)
    const now = () => new Date(at)
    const send = vi.fn<Send>(() => Promise.reject(new TypeError('Failed to fetch')))
    const { box } = make(send, memoryStore(), now)
    await box.recordCompletion(ALICE, EP)

    for (let attempt = 0; attempt < STUCK_ATTEMPTS; attempt += 1) await box.flush()
    expect(box.getSnapshot().stuck.count).toBe(0) // not long enough yet

    at += STUCK_AFTER_MS
    await box.refresh()

    expect(box.getSnapshot().stuck).toEqual({ count: 1, lastError: 'Failed to fetch' })
    expect(await box.all()).toHaveLength(1)
  })

  it('sends the device clock with the batch', async () => {
    const send = answering()
    const { box } = make(send)
    await box.recordCompletion(ALICE, EP)

    await box.flush()

    expect(send.mock.calls[0]?.[0].sent_at).toMatch(/^\d{4}-\d\d-\d\dT/)
  })

  it('takes the cross-tab lock around a flush when there is one (S4)', async () => {
    const names: string[] = []
    const locks: Locks = {
      request: (name, callback) => {
        names.push(name)
        return callback()
      },
    }
    const box = new Outbox({
      store: memoryStore(),
      send: answering(),
      persistent: () => Promise.resolve(true),
      locks,
    })
    box.setOwner(ALICE)
    await box.recordCompletion(ALICE, EP)

    await box.flush()

    expect(names).toEqual([LOCK_NAME])
    expect(await box.all()).toEqual([])
  })

  it('does not notify when nothing in the snapshot changed (N2)', async () => {
    const { box } = make()
    await box.recordCompletion(ALICE, EP)
    const listener = vi.fn()
    box.subscribe(listener)

    await box.refresh()
    await box.refresh()

    expect(listener).not.toHaveBeenCalled()
  })

  it('tells its listeners what a landed flush removed', async () => {
    const { box } = make()
    await box.recordCompletion(ALICE, EP)
    const listener = vi.fn()
    box.onFlushed(listener)

    await box.flush()

    expect(listener).toHaveBeenCalledWith(expect.objectContaining({ ok: true, removed: 1 }))
  })

  it('says when the queue is memory-only', async () => {
    const box = new Outbox({
      store: memoryStore(),
      persistent: () => Promise.resolve(false),
      locks: null,
    })
    box.setOwner(ALICE)
    await box.recordPosition(ALICE, EP, 100, DURATION)

    expect(box.getSnapshot()).toMatchObject({ persistent: false, waiting: 1 })
  })
})

describe('announcing what a replay settled (owner, 2026-10-08)', () => {
  async function replay(status: ItemStatus, record: (box: Outbox) => Promise<void>) {
    const { box } = make(answering(() => status))
    const heard: [number, number, boolean][] = []
    box.onWatched((user, episode, watched) => heard.push([user, episode, watched]))
    await record(box)
    heard.length = 0
    await box.flush()
    return heard
  }

  it('an applied completion is an accepted watch', async () => {
    expect(await replay('applied', (box) => box.recordCompletion(ALICE, EP))).toEqual([
      [ALICE, EP, true],
    ])
  })

  it('an applied position past the mark is one too; one before it is not', async () => {
    expect(
      await replay('applied', (box) =>
        box.recordPosition(ALICE, EP, 1300, DURATION, { completion: false }),
      ),
    ).toEqual([[ALICE, EP, true]])
    expect(await replay('applied', (box) => box.recordPosition(ALICE, EP, 60, DURATION))).toEqual(
      [],
    )
  })

  it('a stale completion (un-marked later) and a retried one say nothing', async () => {
    expect(await replay('stale', (box) => box.recordCompletion(ALICE, EP))).toEqual([])
    expect(await replay('retry', (box) => box.recordCompletion(ALICE, EP))).toEqual([])
  })

  it('an applied un-mark is an un-mark', async () => {
    expect(await replay('applied', (box) => box.recordUnmark(ALICE, EP))).toEqual([
      [ALICE, EP, false],
    ])
  })
})

describe('a synced position is what the server now holds (FR-S2, 2026-10-08)', () => {
  it('tells onApplied of applied records only, and the resume rule notes the position', async () => {
    const heard: [string, number | undefined][] = []
    const { box } = make(answering((item) => (item.kind === 'position' ? 'applied' : 'stale')))
    box.onApplied((record) => {
      heard.push([record.kind, record.position_s])
      notePositionApplied(record)
    })
    await box.recordPosition(ALICE, EP, 488, DURATION, { completion: false })
    await box.recordUnmark(ALICE, EP)
    await box.flush()

    expect(heard).toEqual([['position', 488]])
    await vi.waitFor(async () => {
      expect(await recallServerPosition(ALICE, EP)).toMatchObject({
        position_s: 488,
        duration_s: DURATION,
      })
    })
  })
})
