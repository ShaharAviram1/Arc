import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { animeQueryKey } from '@/lib/anime'
import { acquisitionStatusQueryKey } from '@/lib/acquisition'
import { authMeQueryKey } from '@/lib/auth'
import {
  ART_EVENT,
  COALESCE_MS,
  EPISODE_STATE_EVENT,
  EVENTS_PATH,
  HIDDEN_GRACE_MS,
  parseEvent,
  staleKeys,
  useLiveEvents,
  type ArcEvent,
} from '@/lib/events'
import { createQueryClient } from '@/lib/queryClient'
import { HOME_QUERY_KEY } from '@/lib/schedule'
import { mockApi, TEST_ADMIN, TEST_USER } from '@/test/apiMock'

/**
 * A stand-in for the browser's `EventSource`.
 *
 * jsdom has none, so the hook does nothing at all without this — which is
 * itself the behaviour one of the tests below asserts.
 */
class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly url: string
  closed = false
  private readonly listeners = new Set<(event: MessageEvent) => void>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    if (type === 'message') this.listeners.add(listener)
  }

  removeEventListener(type: string, listener: (event: MessageEvent) => void): void {
    if (type === 'message') this.listeners.delete(listener)
  }

  close(): void {
    this.closed = true
  }

  /** One `data:` frame, as the server writes it. */
  emit(payload: unknown): void {
    const event = new MessageEvent('message', { data: JSON.stringify(payload) })
    for (const listener of [...this.listeners]) listener(event)
  }

  /** The one open stream, for a test that has just made one. */
  static live(): FakeEventSource {
    const open = FakeEventSource.instances.filter((source) => !source.closed)
    expect(open).toHaveLength(1)
    return open[0] as FakeEventSource
  }
}

function event(over: Partial<ArcEvent> = {}): ArcEvent {
  return {
    kind: EPISODE_STATE_EVENT,
    anime_id: 11,
    episode_id: 3,
    state: 'ready',
    ts: '2026-09-13T10:00:00+00:00',
    ...over,
  }
}

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children)
  }
}

/**
 * A client already holding the signed-in user, so the hook's effect runs on
 * the first render: `useMe` answers out of the cache and nothing has to be
 * awaited under fake timers.
 */
function signedIn(admin = false): QueryClient {
  const client = createQueryClient()
  client.setQueryData(authMeQueryKey, admin ? TEST_ADMIN : TEST_USER)
  return client
}

function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  FakeEventSource.instances = []
  mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
  vi.stubGlobal('EventSource', FakeEventSource)
  setVisibility('visible')
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('parseEvent', () => {
  it('reads a frame the server wrote', () => {
    expect(parseEvent(JSON.stringify(event()))).toEqual(event())
  })

  it('drops anything that is not an Arc event, rather than throwing', () => {
    // The channel is a database channel: a client that throws inside
    // `onmessage` loses the rest of the stream.
    expect(parseEvent('not json')).toBeNull()
    expect(parseEvent(JSON.stringify({ kind: 'episode_state' }))).toBeNull()
    expect(parseEvent(JSON.stringify({ anime_id: 1 }))).toBeNull()
    expect(parseEvent(JSON.stringify([1, 2]))).toBeNull()
    expect(parseEvent(42)).toBeNull()
  })
})

describe('staleKeys', () => {
  it('makes Watch Now and the show page stale on a state change', () => {
    expect(staleKeys(event(), { isAdmin: false })).toEqual([[HOME_QUERY_KEY], animeQueryKey(11)])
  })

  it('adds the acquisition status only for the admin who can see it', () => {
    expect(staleKeys(event(), { isAdmin: true })).toEqual([
      [HOME_QUERY_KEY],
      animeQueryKey(11),
      acquisitionStatusQueryKey,
    ])
  })

  it('makes only the show page stale when artwork lands', () => {
    expect(
      staleKeys(event({ kind: ART_EVENT, episode_id: null, state: null }), { isAdmin: true }),
    ).toEqual([animeQueryKey(11)])
  })

  it('makes nothing stale for a kind it does not know', () => {
    expect(staleKeys(event({ kind: 'something_new' }), { isAdmin: true })).toEqual([])
  })
})

describe('useLiveEvents', () => {
  it('opens one stream per tab', () => {
    const client = signedIn()

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.live().url).toBe(EVENTS_PATH)
  })

  it('opens nothing while nobody is signed in', () => {
    mockApi({ 'GET /api/auth/me': { status: 401, body: { detail: 'not authenticated' } } })
    const client = createQueryClient()
    client.setQueryData(authMeQueryKey, null)

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })

    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('closes the stream when the shell unmounts', () => {
    const client = signedIn()

    const { unmount } = renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    const source = FakeEventSource.live()
    unmount()

    expect(source.closed).toBe(true)
  })

  it('invalidates Watch Now and the show on an episode state change', () => {
    vi.useFakeTimers()
    const client = signedIn()
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    FakeEventSource.live().emit(event())

    // Nothing goes out before the window closes.
    expect(invalidate).not.toHaveBeenCalled()
    vi.advanceTimersByTime(COALESCE_MS)

    expect(invalidate.mock.calls.map(([arg]) => arg)).toEqual([
      { queryKey: [HOME_QUERY_KEY] },
      { queryKey: animeQueryKey(11) },
    ])
  })

  it('coalesces a burst into one round of invalidation', () => {
    vi.useFakeTimers()
    const client = signedIn()
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    const source = FakeEventSource.live()
    // What one `compute_wants` tick looks like: several episodes of two shows.
    source.emit(event({ episode_id: 1, state: 'wanted' }))
    source.emit(event({ episode_id: 2, state: 'wanted' }))
    source.emit(event({ anime_id: 12, episode_id: 9, state: 'searching' }))
    vi.advanceTimersByTime(COALESCE_MS)

    expect(invalidate.mock.calls.map(([arg]) => arg)).toEqual([
      { queryKey: [HOME_QUERY_KEY] },
      { queryKey: animeQueryKey(11) },
      { queryKey: animeQueryKey(12) },
    ])
  })

  it('keeps the stream through a short tab switch and drops it after a long one', () => {
    vi.useFakeTimers()
    const client = signedIn()

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    const source = FakeEventSource.live()

    setVisibility('hidden')
    vi.advanceTimersByTime(HIDDEN_GRACE_MS - 1)
    expect(source.closed).toBe(false)

    vi.advanceTimersByTime(1)
    expect(source.closed).toBe(true)
  })

  it('starts the grace timer for a tab that was already hidden at mount', () => {
    // `visibilitychange` never fires for a state that was already true, so a
    // tab opened in the background (or restored into a window that is not the
    // front one) would otherwise hold its stream for as long as it stayed
    // hidden — the one case the grace timer exists to stop.
    vi.useFakeTimers()
    setVisibility('hidden')
    const client = signedIn()

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    const source = FakeEventSource.instances[0]
    expect(source?.closed).toBe(false)

    vi.advanceTimersByTime(HIDDEN_GRACE_MS)

    expect(source?.closed).toBe(true)
  })

  it('flushes what it had collected when the stream pauses or the shell goes', () => {
    // An event that arrives 200ms before a tab is hidden must not be the one
    // event the app never acts on: the keys are already known to be stale.
    vi.useFakeTimers()
    const client = signedIn()
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    const { unmount } = renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    FakeEventSource.live().emit(event())
    expect(invalidate).not.toHaveBeenCalled()

    unmount()

    expect(invalidate.mock.calls.map(([arg]) => arg)).toEqual([
      { queryKey: [HOME_QUERY_KEY] },
      { queryKey: animeQueryKey(11) },
    ])
  })

  it('re-opens and catches up once when the tab comes back', () => {
    vi.useFakeTimers()
    const client = signedIn()

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    client.setQueryData(animeQueryKey(11), { id: 11 })
    setVisibility('hidden')
    vi.advanceTimersByTime(HIDDEN_GRACE_MS)

    const invalidate = vi.spyOn(client, 'invalidateQueries')
    setVisibility('visible')

    // A fresh stream, and exactly one catch-up: Watch Now and the show
    // details, never the searches that share the `anime` prefix.
    expect(FakeEventSource.live().closed).toBe(false)
    expect(invalidate).toHaveBeenCalledTimes(2)
    expect(invalidate.mock.calls[0]?.[0]).toEqual({ queryKey: [HOME_QUERY_KEY] })
  })

  it('asks for the acquisition status too when an admin is watching', () => {
    vi.useFakeTimers()
    const client = signedIn(true)
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    FakeEventSource.live().emit(event())
    vi.advanceTimersByTime(COALESCE_MS)

    expect(invalidate.mock.calls.map(([arg]) => arg)).toContainEqual({
      queryKey: acquisitionStatusQueryKey,
    })
  })

  it('leaves the polling alone when the stream fails', () => {
    vi.useFakeTimers()
    const client = signedIn()
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })
    const source = FakeEventSource.live()
    // `EventSource` reports an error and reconnects on its own; the hook
    // neither listens for it nor changes anything about the queries, which is
    // what leaves every `refetchInterval` in `anime.ts` as the fallback.
    source.emit('not an event at all')
    vi.advanceTimersByTime(COALESCE_MS)

    expect(invalidate).not.toHaveBeenCalled()
    expect(source.closed).toBe(false)
  })

  it('does nothing at all where the browser has no EventSource', () => {
    vi.stubGlobal('EventSource', undefined)
    const client = signedIn()

    renderHook(() => useLiveEvents(), { wrapper: wrapperFor(client) })

    expect(FakeEventSource.instances).toHaveLength(0)
  })
})
