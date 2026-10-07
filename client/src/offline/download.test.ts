import { describe, expect, it, vi } from 'vitest'
import {
  BACKOFF_MS,
  delayFor,
  MAX_RESTARTS,
  parseContentRange,
  runDownload,
  STEADY_BACKOFF_MS,
  type DownloadDeps,
  type StartCommand,
  type SyncHandle,
  type WorkerMessage,
} from '@/offline/download'

/** A growable in-memory stand-in for an OPFS sync access handle. */
class FakeHandle implements SyncHandle {
  bytes = new Uint8Array(0)
  closed = false
  quotaAt: number | null = null

  constructor(initial?: Uint8Array) {
    if (initial !== undefined) this.bytes = initial.slice()
  }

  getSize(): number {
    return this.bytes.byteLength
  }

  write(data: Uint8Array, options: { at: number }): number {
    const end = options.at + data.byteLength
    if (this.quotaAt !== null && end > this.quotaAt) {
      // Part of the chunk lands before the disk says no, as it can for real.
      this.grow(this.quotaAt)
      this.bytes.set(data.subarray(0, this.quotaAt - options.at), options.at)
      throw new DOMException('full', 'QuotaExceededError')
    }
    this.grow(end)
    this.bytes.set(data, options.at)
    return data.byteLength
  }

  private grow(size: number): void {
    if (size <= this.bytes.byteLength) return
    const next = new Uint8Array(size)
    next.set(this.bytes)
    this.bytes = next
  }

  truncate(size: number): void {
    this.bytes = this.bytes.slice(0, size)
  }

  flush(): void {}

  close(): void {
    this.closed = true
  }
}

function content(size: number, seed = 0): Uint8Array<ArrayBuffer> {
  return Uint8Array.from({ length: size }, (_, index) => (index + seed) % 251)
}

/** A body that records whether anybody read it, and whether it was cancelled. */
function trackedResponse(
  body: Uint8Array,
  init: ResponseInit,
): { response: Response; read: () => boolean; cancelled: () => boolean } {
  let read = false
  let cancelled = false
  const stream = new ReadableStream<Uint8Array>(
    {
      pull(controller) {
        read = true
        controller.enqueue(body)
        controller.close()
      },
      cancel() {
        cancelled = true
      },
      // No eager pull: a chunk is produced only when somebody reads.
    },
    { highWaterMark: 0 },
  )
  return {
    response: new Response(stream, init),
    read: () => read,
    cancelled: () => cancelled,
  }
}

interface ServerFile {
  bytes: Uint8Array
  etag: string
}

/** The §5.4a contract, as the server implements it: Range, If-Range, ETag. */
function rangeServer(file: () => ServerFile) {
  return vi.fn((_url: string, init: RequestInit): Promise<Response> => {
    const headers = init.headers as Record<string, string>
    const { bytes, etag } = file()
    const total = bytes.byteLength
    const ifRange = headers['If-Range']
    if (ifRange !== undefined && ifRange !== etag) {
      return Promise.resolve(new Response(bytes.slice(), { status: 200, headers: { ETag: etag } }))
    }
    const match = /^bytes=(\d+)-(\d+)$/.exec(headers.Range ?? '')
    const first = Number(match?.[1] ?? 0)
    if (first >= total) {
      return Promise.resolve(
        new Response(null, {
          status: 416,
          headers: { ETag: etag, 'Content-Range': `bytes */${String(total)}` },
        }),
      )
    }
    const last = Math.min(Number(match?.[2] ?? total - 1), total - 1)
    return Promise.resolve(
      new Response(bytes.slice(first, last + 1), {
        status: 206,
        headers: {
          ETag: etag,
          'Content-Range': `bytes ${String(first)}-${String(last)}/${String(total)}`,
        },
      }),
    )
  })
}

const COMMAND: StartCommand = {
  cmd: 'download',
  name: 'episode-9001.mp4',
  url: '/media/9001/episode.mp4',
  etag: null,
  total: null,
  fresh: false,
}

function harness(overrides: Partial<DownloadDeps> & { handle?: FakeHandle } = {}): {
  deps: DownloadDeps
  handle: FakeHandle
  messages: WorkerMessage[]
  sleeps: number[]
} {
  const handle = overrides.handle ?? new FakeHandle()
  const messages: WorkerMessage[] = []
  const sleeps: number[] = []
  const deps: DownloadDeps = {
    fetch: overrides.fetch ?? rangeServer(() => ({ bytes: content(25), etag: '"a"' })),
    open: () => Promise.resolve(handle),
    post: (message) => {
      messages.push(message)
    },
    sleep: (ms) => {
      sleeps.push(ms)
      return Promise.resolve()
    },
    stopped: overrides.stopped ?? (() => false),
    chunkBytes: overrides.chunkBytes ?? 10,
  }
  return { deps, handle, messages, sleeps }
}

function last(messages: WorkerMessage[]): WorkerMessage | undefined {
  return messages[messages.length - 1]
}

describe('parseContentRange', () => {
  it('reads a span and the unsatisfied form', () => {
    expect(parseContentRange('bytes 0-9/25')).toEqual({ first: 0, last: 9, total: 25 })
    expect(parseContentRange('bytes */25')).toEqual({ first: null, last: null, total: 25 })
    expect(parseContentRange('nonsense')).toBeNull()
    expect(parseContentRange(null)).toBeNull()
  })
})

describe('runDownload', () => {
  it('fetches the file in ranged chunks and checks the finished size', async () => {
    const file = content(25)
    const { deps, handle, messages } = harness({
      fetch: rangeServer(() => ({ bytes: file, etag: '"a"' })),
    })

    await runDownload(COMMAND, deps)

    expect(handle.bytes).toEqual(file)
    expect(messages.filter((m) => m.type === 'progress').map((m) => m.offset)).toEqual([10, 20, 25])
    expect(last(messages)).toEqual({
      type: 'done',
      name: COMMAND.name,
      offset: 25,
      total: 25,
      etag: '"a"',
    })
    expect(handle.closed).toBe(true)
  })

  it('sends the session cookie and, once it has one, If-Range with the ETag', async () => {
    const fetch = rangeServer(() => ({ bytes: content(25), etag: '"a"' }))
    const { deps } = harness({ fetch })

    await runDownload(COMMAND, deps)

    const inits = fetch.mock.calls.map(([, init]) => init)
    expect(inits.every((init) => init.credentials === 'include')).toBe(true)
    expect((inits[0]?.headers as Record<string, string>)['If-Range']).toBeUndefined()
    expect((inits[1]?.headers as Record<string, string>)['If-Range']).toBe('"a"')
  })

  it('resumes from the size of the file on disk, not from anything recorded', async () => {
    const file = content(25)
    const fetch = rangeServer(() => ({ bytes: file, etag: '"a"' }))
    const { deps, handle, messages } = harness({
      fetch,
      handle: new FakeHandle(file.slice(0, 20)),
    })

    await runDownload({ ...COMMAND, etag: '"a"', total: 25 }, deps)

    expect((fetch.mock.calls[0]?.[1].headers as Record<string, string>).Range).toBe('bytes=20-29')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(handle.bytes).toEqual(file)
    expect(last(messages)?.type).toBe('done')
  })

  it('confirms a whole file with one small If-Range request before calling it done', async () => {
    // Another account on this device kept the same episode: the bytes are
    // there, but this account must be allowed it and the encode be current.
    const file = content(25)
    const fetch = rangeServer(() => ({ bytes: file, etag: '"a"' }))
    const { deps, messages } = harness({ fetch, handle: new FakeHandle(file) })

    await runDownload({ ...COMMAND, etag: '"a"', total: 25 }, deps)

    expect(fetch).toHaveBeenCalledTimes(1)
    const headers = fetch.mock.calls[0]?.[1].headers as Record<string, string>
    expect(headers).toEqual({ Range: 'bytes=24-24', 'If-Range': '"a"' })
    expect(last(messages)).toMatchObject({ type: 'done', total: 25 })
  })

  it('starts a whole file over when that check finds a newer encode', async () => {
    const fresh = content(25, 9)
    const fetch = rangeServer(() => ({ bytes: fresh, etag: '"b"' }))
    const { deps, handle, messages } = harness({ fetch, handle: new FakeHandle(content(25)) })

    await runDownload({ ...COMMAND, etag: '"a"', total: 25 }, deps)

    expect(messages).toContainEqual({ type: 'restarted', name: COMMAND.name, reason: 'replaced' })
    expect(handle.bytes).toEqual(fresh)
  })

  it('does not hand a whole file to an account the server refuses', async () => {
    const fetch = vi.fn(() => Promise.resolve(new Response('no', { status: 403 })))
    const { deps, messages } = harness({ fetch, handle: new FakeHandle(content(25)) })

    await runDownload({ ...COMMAND, etag: '"a"', total: 25 }, deps)

    expect(last(messages)).toMatchObject({ type: 'failed', code: 'auth' })
  })

  it('throws away whatever the file held when told to start fresh', async () => {
    const file = content(25)
    const fetch = rangeServer(() => ({ bytes: file, etag: '"a"' }))
    // An orphan of a delete: bytes no record vouches for.
    const { deps, handle } = harness({ fetch, handle: new FakeHandle(content(20, 5)) })

    await runDownload({ ...COMMAND, fresh: true }, deps)

    expect((fetch.mock.calls[0]?.[1].headers as Record<string, string>).Range).toBe('bytes=0-9')
    expect(handle.bytes).toEqual(file)
  })

  it('posts its last message only after the file is closed', async () => {
    const handle = new FakeHandle()
    const closedAtEnd: boolean[] = []
    const { deps } = harness({ handle })
    const post = deps.post
    deps.post = (message) => {
      if (message.type === 'done' || message.type === 'paused' || message.type === 'failed') {
        closedAtEnd.push(handle.closed)
      }
      post(message)
    }

    await runDownload(COMMAND, deps)

    expect(closedAtEnd).toEqual([true])
  })

  it('aborts a 200 to a ranged request unread and starts again from zero', async () => {
    // The episode was re-encoded while paused: If-Range no longer matches.
    const fresh = content(25, 7)
    const whole = trackedResponse(fresh, { status: 200, headers: { ETag: '"b"' } })
    let first = true
    const server = rangeServer(() => ({ bytes: fresh, etag: '"b"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      if (first) {
        first = false
        return Promise.resolve(whole.response)
      }
      return server(url, init)
    })
    const { deps, handle, messages } = harness({
      fetch,
      handle: new FakeHandle(content(20)),
    })

    await runDownload({ ...COMMAND, etag: '"a"', total: 25 }, deps)

    expect(whole.read()).toBe(false)
    expect(whole.cancelled()).toBe(true)
    expect(messages).toContainEqual({ type: 'restarted', name: COMMAND.name, reason: 'replaced' })
    expect((fetch.mock.calls[1]?.[1].headers as Record<string, string>).Range).toBe('bytes=0-9')
    expect(handle.bytes).toEqual(fresh)
    expect(last(messages)).toMatchObject({ type: 'done', etag: '"b"', total: 25 })
  })

  it('restarts when the ETag changes between chunks', async () => {
    const before = content(25)
    const after = content(25, 3)
    let calls = 0
    const fetch = rangeServer(() => {
      calls += 1
      return calls <= 1 ? { bytes: before, etag: '"a"' } : { bytes: after, etag: '"b"' }
    })
    // Without If-Range the server would happily answer 206 from the new file,
    // so the check on the response's own ETag is what catches it.
    const noIfRange = vi.fn((url: string, init: RequestInit) => {
      const headers = { ...(init.headers as Record<string, string>) }
      delete headers['If-Range']
      return fetch(url, { ...init, headers })
    })
    const { deps, handle, messages } = harness({ fetch: noIfRange })

    await runDownload(COMMAND, deps)

    expect(messages).toContainEqual({ type: 'restarted', name: COMMAND.name, reason: 'etag' })
    expect(handle.bytes).toEqual(after)
    expect(last(messages)).toMatchObject({ type: 'done', etag: '"b"' })
  })

  it('restarts when the Content-Range total changes', async () => {
    let calls = 0
    const fetch = rangeServer(() => {
      calls += 1
      return { bytes: calls <= 1 ? content(25) : content(31), etag: '"same"' }
    })
    const { deps, handle, messages } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(messages).toContainEqual({ type: 'restarted', name: COMMAND.name, reason: 'total' })
    expect(handle.getSize()).toBe(31)
    expect(last(messages)).toMatchObject({ type: 'done', total: 31 })
  })

  it('gives up on a server that never honours Range', async () => {
    const fetch = vi.fn(() =>
      Promise.resolve(new Response(content(25), { status: 200, headers: { ETag: '"a"' } })),
    )
    const { deps, messages } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(fetch).toHaveBeenCalledTimes(MAX_RESTARTS)
    expect(last(messages)).toMatchObject({ type: 'failed', code: 'error' })
  })

  it('backs off 1 s, 3 s, 10 s and then a steady 30 s while the network is gone', async () => {
    let failures = 5
    const server = rangeServer(() => ({ bytes: content(5), etag: '"a"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      if (failures > 0) {
        failures -= 1
        return Promise.reject(new TypeError('Load failed'))
      }
      return server(url, init)
    })
    const { deps, messages, sleeps } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(sleeps).toEqual([...BACKOFF_MS, STEADY_BACKOFF_MS, STEADY_BACKOFF_MS])
    expect(messages.filter((m) => m.type === 'retrying').map((m) => m.attempt)).toEqual([
      1, 2, 3, 4, 5,
    ])
    expect(last(messages)?.type).toBe('done')
    expect(delayFor(10)).toBe(STEADY_BACKOFF_MS)
  })

  it('retries a 5xx rather than stopping', async () => {
    let first = true
    const server = rangeServer(() => ({ bytes: content(5), etag: '"a"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      if (first) {
        first = false
        return Promise.resolve(new Response('busy', { status: 503 }))
      }
      return server(url, init)
    })
    const { deps, messages } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(messages.some((m) => m.type === 'retrying')).toBe(true)
    expect(last(messages)?.type).toBe('done')
  })

  it.each([
    [401, 'auth'],
    [403, 'auth'],
    [404, 'gone'],
  ] as const)('stops for good on a %i', async (status, code) => {
    const fetch = vi.fn(() => Promise.resolve(new Response('no', { status })))
    const { deps, messages, sleeps } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(sleeps).toEqual([])
    expect(last(messages)).toMatchObject({ type: 'failed', code })
  })

  it('never reads a 206 whose span runs past the range it asked for', async () => {
    const oversized = trackedResponse(content(40), {
      status: 206,
      headers: { ETag: '"a"', 'Content-Range': 'bytes 0-39/40' },
    })
    let first = true
    const server = rangeServer(() => ({ bytes: content(40), etag: '"a"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      if (first) {
        first = false
        return Promise.resolve(oversized.response)
      }
      return server(url, init)
    })
    const { deps, handle, messages } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(oversized.read()).toBe(false)
    expect(messages).toContainEqual({ type: 'restarted', name: COMMAND.name, reason: 'range' })
    expect(handle.getSize()).toBe(40)
    expect(last(messages)?.type).toBe('done')
  })

  it('never reads a 206 whose Content-Length disagrees with its span', async () => {
    const lying = trackedResponse(content(10), {
      status: 206,
      headers: { ETag: '"a"', 'Content-Range': 'bytes 0-9/25', 'Content-Length': '999' },
    })
    let first = true
    const server = rangeServer(() => ({ bytes: content(25), etag: '"a"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      if (first) {
        first = false
        return Promise.resolve(lying.response)
      }
      return server(url, init)
    })
    const { deps, messages } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(lying.read()).toBe(false)
    expect(last(messages)?.type).toBe('done')
  })

  it('aborts the request it abandons', async () => {
    let signal: AbortSignal | undefined
    let first = true
    const server = rangeServer(() => ({ bytes: content(5), etag: '"b"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      if (first) {
        first = false
        signal = init.signal ?? undefined
        return Promise.resolve(new Response(content(5), { status: 200, headers: { ETag: '"b"' } }))
      }
      return server(url, init)
    })
    const { deps } = harness({ fetch })

    await runDownload({ ...COMMAND, etag: '"a"' }, deps)

    expect(signal?.aborted).toBe(true)
  })

  it('stops for good on a 400', async () => {
    const fetch = vi.fn(() => Promise.resolve(new Response('bad', { status: 400 })))
    const { deps, messages, sleeps } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(sleeps).toEqual([])
    expect(last(messages)).toMatchObject({ type: 'failed', code: 'error' })
  })

  it('waits at least as long as a 429 says', async () => {
    let first = true
    const server = rangeServer(() => ({ bytes: content(5), etag: '"a"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      if (first) {
        first = false
        return Promise.resolve(
          new Response('slow down', { status: 429, headers: { 'Retry-After': '20' } }),
        )
      }
      return server(url, init)
    })
    const { deps, sleeps } = harness({ fetch })

    await runDownload(COMMAND, deps)

    expect(sleeps).toEqual([20_000])
  })

  it('fails the size check when the bytes on disk do not add up to the total', async () => {
    // A disk that accepts a write and keeps none of it.
    const handle = new FakeHandle()
    handle.write = (data) => data.byteLength
    const fetch = rangeServer(() => ({ bytes: content(5), etag: '"a"' }))
    const { deps, messages } = harness({ fetch, handle })

    await runDownload(COMMAND, deps)

    expect(last(messages)).toMatchObject({ type: 'failed', code: 'size', offset: 0 })
  })

  it('pauses on a full disk (never fails) and leaves a resumable file', async () => {
    const handle = new FakeHandle()
    handle.quotaAt = 15
    const { deps, messages } = harness({ handle })

    await runDownload(COMMAND, deps)

    expect(last(messages)).toMatchObject({ type: 'paused', reason: 'quota', offset: 10 })
    expect(messages.some((m) => m.type === 'failed')).toBe(false)
    // The part of the chunk that landed was cut back to the chunk boundary.
    expect(handle.getSize()).toBe(10)
    expect(handle.closed).toBe(true)
  })

  it('stops at a chunk boundary when paused, and says how far it got', async () => {
    let chunks = 0
    const server = rangeServer(() => ({ bytes: content(25), etag: '"a"' }))
    const fetch = vi.fn((url: string, init: RequestInit) => {
      chunks += 1
      return server(url, init)
    })
    const { deps, messages } = harness({ fetch, stopped: () => chunks >= 1 })

    await runDownload(COMMAND, deps)

    expect(last(messages)).toEqual({ type: 'paused', name: COMMAND.name, offset: 10 })
  })
})

/**
 * A handle the device closed under the worker (owner incident, 2026-10-07):
 * iPadOS closes a worker's sync access handle when Arc leaves the screen, and
 * a full iPad fails every write the same way. One file on "disk", any number
 * of handles opened on it in turn.
 */
class FakeDisk {
  bytes = new Uint8Array(0)
  handles: StaleHandle[] = []
  /** How many writes, across handles, throw `InvalidStateError` before one lands. */
  staleWrites = 0
  /** Bytes of a failing write that land before it throws. */
  partial = 0
  /** What the file shrinks to when it is next opened, as if flushes were lost. */
  shrinkOnOpen: number | null = null

  open = (): Promise<SyncHandle> => {
    if (this.shrinkOnOpen !== null && this.handles.length > 0) {
      this.bytes = this.bytes.slice(0, this.shrinkOnOpen)
      this.shrinkOnOpen = null
    }
    const handle = new StaleHandle(this)
    this.handles.push(handle)
    return Promise.resolve(handle)
  }
}

class StaleHandle implements SyncHandle {
  closed = false
  private readonly disk: FakeDisk

  constructor(disk: FakeDisk) {
    this.disk = disk
  }

  getSize(): number {
    return this.disk.bytes.byteLength
  }

  write(data: Uint8Array, options: { at: number }): number {
    if (this.closed || this.disk.staleWrites > 0) {
      this.disk.staleWrites -= 1
      if (this.disk.partial > 0) this.put(data.subarray(0, this.disk.partial), options.at)
      this.closed = true
      throw new DOMException('failed to write to file', 'InvalidStateError')
    }
    this.put(data, options.at)
    return data.byteLength
  }

  private put(data: Uint8Array, at: number): void {
    const end = at + data.byteLength
    if (end > this.disk.bytes.byteLength) {
      const next = new Uint8Array(end)
      next.set(this.disk.bytes)
      this.disk.bytes = next
    }
    this.disk.bytes.set(data, at)
  }

  truncate(size: number): void {
    if (this.closed) throw new DOMException('closed', 'InvalidStateError')
    this.disk.bytes = this.disk.bytes.slice(0, size)
  }

  flush(): void {
    if (this.closed) throw new DOMException('closed', 'InvalidStateError')
  }

  close(): void {
    this.closed = true
  }
}

describe('runDownload with a handle the device closed', () => {
  function staleHarness(disk: FakeDisk, file = content(25)) {
    const fetch = rangeServer(() => ({ bytes: file, etag: '"a"' }))
    const { deps, messages } = harness({ fetch })
    deps.open = disk.open
    return { deps, messages, fetch, file }
  }

  it('re-opens the file, retries the chunk and finishes with the right bytes', async () => {
    const disk = new FakeDisk()
    const { deps, messages, file } = staleHarness(disk)
    // The second chunk's write is refused once, after 3 of its bytes landed.
    let writes = 0
    const open = disk.open
    deps.open = async () => {
      const handle = await open()
      const write = handle.write.bind(handle)
      handle.write = (data, options) => {
        writes += 1
        if (writes === 2) {
          disk.staleWrites = 1
          disk.partial = 3
        }
        return write(data, options)
      }
      return handle
    }

    await runDownload(COMMAND, deps)

    expect(disk.bytes).toEqual(file)
    expect(disk.handles).toHaveLength(2)
    expect(disk.handles.every((handle) => handle.closed)).toBe(true)
    expect(last(messages)).toMatchObject({ type: 'done', offset: 25, total: 25 })
  })

  it('pauses as interrupted when the re-opened file refuses the chunk too', async () => {
    const disk = new FakeDisk()
    disk.staleWrites = Number.POSITIVE_INFINITY
    const { deps, messages } = staleHarness(disk)

    await runDownload(COMMAND, deps)

    expect(last(messages)).toMatchObject({
      type: 'paused',
      reason: 'interrupted',
      offset: 0,
      detail: expect.stringMatching(/InvalidStateError/) as unknown,
    })
    expect(messages.some((m) => m.type === 'failed')).toBe(false)
    // Opened once, re-opened once, and let go of.
    expect(disk.handles).toHaveLength(2)
    expect(disk.handles.every((handle) => handle.closed)).toBe(true)
  })

  it('fetches the chunk again when the re-opened file is shorter than the offset', async () => {
    const disk = new FakeDisk()
    const { deps, messages, fetch, file } = staleHarness(disk)
    let writes = 0
    const open = disk.open
    deps.open = async () => {
      const handle = await open()
      const write = handle.write.bind(handle)
      handle.write = (data, options) => {
        writes += 1
        if (writes === 2) {
          disk.staleWrites = 1
          disk.shrinkOnOpen = 4
        }
        return write(data, options)
      }
      return handle
    }

    await runDownload(COMMAND, deps)

    const ranges = fetch.mock.calls.map(
      ([, init]) => (init.headers as Record<string, string>).Range,
    )
    expect(ranges).toEqual(['bytes=0-9', 'bytes=10-19', 'bytes=4-13', 'bytes=14-23', 'bytes=24-33'])
    // The same validator on the fetch after the re-open.
    expect((fetch.mock.calls[2]?.[1].headers as Record<string, string>)['If-Range']).toBe('"a"')
    expect(disk.bytes).toEqual(file)
    expect(last(messages)).toMatchObject({ type: 'done', offset: 25 })
  })

  it('fails a write that throws anything else, and says the file could not be written', async () => {
    const handle = new FakeHandle()
    handle.write = () => {
      throw new Error('disk on fire')
    }
    const { deps, messages } = harness({ handle })

    await runDownload(COMMAND, deps)

    expect(last(messages)).toMatchObject({ type: 'failed', code: 'error', write: true })
    expect(handle.closed).toBe(true)
  })
})
