import { afterEach, describe, expect, it, vi } from 'vitest'
import { sendProgress, sendWatched, shouldQueue } from '@/lib/playback'
import { ApiError } from '@/lib/api'
import { Outbox, setOutbox } from '@/offline/outbox'
import { ProgressReporter } from '@/player/ProgressReporter'
import { memoryStore } from '@/offline/store'

const USER = 7
const EP = 42

function box() {
  const outbox = new Outbox({
    store: memoryStore(),
    persistent: () => Promise.resolve(true),
    locks: null,
  })
  outbox.setOwner(USER)
  return outbox
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('progress writes when the network is gone (FR-S8)', () => {
  it('posts to /api/progress as before when online, and queues nothing', async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        jsonResponse({ completed: false, newly_completed: false, list_progress: null }),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const outbox = box()

    const result = await sendProgress({ episode_id: EP, position_s: 30, duration_s: 1420 }, outbox)

    expect(result.queued).toBeUndefined()
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/progress')
    expect(await outbox.all()).toEqual([])
  })

  it('falls into the outbox when fetch throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))),
    )
    const outbox = box()

    const result = await sendProgress(
      { episode_id: EP, position_s: 1300, duration_s: 1420 },
      outbox,
    )

    expect(result).toMatchObject({ queued: true, newly_completed: false })
    const records = await outbox.all()
    expect(records.map((record) => [record.kind, record.user_id, record.episode_id])).toEqual([
      ['position', USER, EP],
      ['completion', USER, EP],
    ])
  })

  it('does not queue what the server refused outright', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ detail: 'episode not found' }, 404))),
    )
    const outbox = box()

    await expect(
      sendProgress({ episode_id: EP, position_s: 1, duration_s: 1420 }, outbox),
    ).rejects.toBeInstanceOf(ApiError)
    expect(await outbox.all()).toEqual([])
  })

  it('queues the manual mark and un-mark as their own records', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('offline'))),
    )
    const outbox = box()

    expect(await sendWatched(EP, true, outbox)).toMatchObject({ completed: true, queued: true })
    expect(await sendWatched(EP, false, outbox)).toMatchObject({ completed: false, queued: true })

    expect((await outbox.all()).map((record) => record.kind)).toEqual(['completion', 'unmark'])
  })

  it('rethrows when no account has signed in to own the record', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('offline'))),
    )
    const outbox = new Outbox({
      store: memoryStore(),
      persistent: () => Promise.resolve(true),
      locks: null,
    })

    await expect(sendWatched(EP, true, outbox)).rejects.toBeInstanceOf(TypeError)
  })

  it('queues on "not now" answers only', () => {
    expect(shouldQueue(new TypeError('Failed to fetch'))).toBe(true)
    expect(shouldQueue(new ApiError(503, null))).toBe(true)
    expect(shouldQueue(new ApiError(401, null))).toBe(true)
    expect(shouldQueue(new ApiError(429, null))).toBe(true)
    expect(shouldQueue(new ApiError(404, null))).toBe(false)
    expect(shouldQueue(new ApiError(422, null))).toBe(false)
  })
})

describe('the exit report after finishing online (B1, 2026-10-05)', () => {
  afterEach(() => {
    setOutbox(null)
  })

  function shared() {
    const outbox = box()
    setOutbox(outbox)
    vi.stubGlobal('navigator', { sendBeacon: vi.fn(() => true), onLine: true })
    return outbox
  }

  it('queues nothing once the server said the episode is completed', async () => {
    const outbox = shared()
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          jsonResponse({ completed: true, newly_completed: true, list_progress: 11 }),
        ),
      ),
    )
    await sendProgress({ episode_id: EP, position_s: 1290, duration_s: 1420 }, outbox)

    const reporter = new ProgressReporter({ episodeId: EP, report: () => undefined })
    reporter.update(1320, 1420) // 93 %
    reporter.destroy()
    // Let anything the reporter might have queued land before looking.
    await new Promise((resolve) => setTimeout(resolve, 10))

    expect(await outbox.all()).toEqual([])
  })

  it('queues a position — never a completion — when the fate of the beacon is unknown', async () => {
    const outbox = shared()

    const reporter = new ProgressReporter({ episodeId: EP, report: () => undefined })
    reporter.update(1320, 1420)
    reporter.destroy()
    await vi.waitFor(async () => {
      expect((await outbox.all()).map((record) => record.kind)).toEqual(['position'])
    })
  })

  it('an un-mark forgets the completed answer', async () => {
    const outbox = shared()
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          jsonResponse({ completed: true, newly_completed: false, list_progress: null }),
        ),
      ),
    )
    await sendWatched(EP, true, outbox)
    expect(outbox.knownCompleted(USER, EP)).toBe(true)
    await sendWatched(EP, false, outbox)
    expect(outbox.knownCompleted(USER, EP)).toBe(false)
  })
})
