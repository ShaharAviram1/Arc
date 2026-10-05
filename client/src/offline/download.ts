/**
 * Fetching one episode into the on-device store, chunk by chunk (spec FR-S9,
 * architecture §5.4d). Ported from Audiosey's downloader, with Arc's own
 * resume contract (architecture §5.4a).
 *
 * 8 MB `Range` requests to `GET /media/{id}/episode.mp4`, written into an OPFS
 * file through a **sync access handle**, with the resume offset taken from the
 * file's own size so nothing recorded separately can disagree with the bytes
 * on disk. A kill costs one chunk.
 *
 * The contract with the server, each rule a way two different encodes could
 * otherwise be spliced into one file:
 *
 * - **A `200` to a ranged request means "this is not the file you were
 *   resuming".** The server answers 200 when `If-Range` no longer matches (the
 *   episode was re-encoded). The body is cancelled unread and the file starts
 *   again from zero.
 * - **A changed `ETag`, or a changed total in `Content-Range`, means the
 *   same.** Cancelled unread, restarted from zero.
 * - **The finished size is checked against the `Content-Range` total.** A body
 *   that was cut short must not pass for an episode.
 *
 * A chunk that fails for want of a network is retried here, not surfaced:
 * {@link BACKOFF_MS} is 1 s, 3 s, 10 s, and after those a steady
 * {@link STEADY_BACKOFF_MS}. The page hears about every retry so it can say
 * something once the quick ones are spent; the loop never gives up on its own.
 * 400 / 401 / 403 / 404 are terminal, and so is a full disk; a 429 waits at
 * least as long as its `Retry-After`.
 *
 * **The terminal message is posted only after the file is closed** (from the
 * `finally`), so a manager that deletes the file on hearing it never races a
 * handle that is still open.
 *
 * Every dependency is injected — `downloadWorker.ts` supplies the real OPFS
 * handle, `fetch` and `postMessage` — so all of the above is tested without a
 * browser.
 */

/** 8 MB: big enough to be efficient, small enough to lose. */
export const CHUNK_BYTES = 8 * 1024 * 1024

/** The quick retries, in order. After these it settles into a steady beat. */
export const BACKOFF_MS = [1000, 3000, 10_000] as const

/** The cadence after the quick retries — and the point the UI says something. */
export const STEADY_BACKOFF_MS = 30_000

/**
 * Restarts in a row, with no chunk landing in between, before the loop stops
 * believing the server can resume at all. One is a re-encode; three is a
 * server (or a proxy in front of it) that ignores `Range`.
 */
export const MAX_RESTARTS = 3

export interface StartCommand {
  cmd: 'download'
  /** The OPFS file name, derived from the episode id by the manager. */
  name: string
  /** The server's `download_url` for the episode. */
  url: string
  /** The ETag a previous run saw, when resuming one. */
  etag: string | null
  /** The total a previous run saw in `Content-Range`, when resuming one. */
  total: number | null
  /**
   * Throw away whatever the file already holds before starting. Set by the
   * manager when no record vouches for the bytes on disk — an orphan from a
   * delete, a file whose copy would not play — so nothing of unknown origin
   * is ever resumed into.
   */
  fresh: boolean
  /**
   * Which run this is, minted by the manager. The worker stamps it on every
   * message, so a late message from a run the manager has already given up on
   * is never mistaken for news about the run that replaced it.
   */
  run?: number
}

export interface PauseCommand {
  cmd: 'pause'
  name: string
}

export type WorkerCommand = StartCommand | PauseCommand

/** Why a download stopped for good — each one is a different sentence in the UI. */
export type FailCode = 'quota' | 'auth' | 'gone' | 'size' | 'error'

export type RestartReason = 'replaced' | 'etag' | 'total' | 'range'

export type WorkerMessage = (
  | { type: 'progress'; name: string; offset: number; total: number; etag: string | null }
  | { type: 'done'; name: string; offset: number; total: number; etag: string | null }
  | { type: 'paused'; name: string; offset: number }
  | { type: 'retrying'; name: string; attempt: number; delay: number; reason: string }
  | { type: 'restarted'; name: string; reason: RestartReason }
  | { type: 'failed'; name: string; offset: number; code: FailCode; reason: string }
  /** Another Arc window holds this file's lock and is downloading it. Terminal. */
  | { type: 'busy'; name: string }
) & { run?: number }

/** The messages after which the worker has let go of the file. */
export type TerminalMessage = Extract<
  WorkerMessage,
  { type: 'done' } | { type: 'paused' } | { type: 'failed' } | { type: 'busy' }
>

export function isTerminal(message: WorkerMessage): message is TerminalMessage {
  return (
    message.type === 'done' ||
    message.type === 'paused' ||
    message.type === 'failed' ||
    message.type === 'busy'
  )
}

/** The bit of `FileSystemSyncAccessHandle` this uses. */
export interface SyncHandle {
  getSize(): number
  write(data: Uint8Array, options: { at: number }): number
  truncate(size: number): void
  flush(): void
  close(): void
}

export interface DownloadDeps {
  fetch: (url: string, init: RequestInit) => Promise<Response>
  open: (name: string) => Promise<SyncHandle>
  post: (message: WorkerMessage) => void
  /** Interruptible, so "the app is on screen again" can cut a backoff short. */
  sleep: (ms: number) => Promise<void>
  /** Checked at the top of every chunk; true once a pause has been asked for. */
  stopped: () => boolean
  /** Overridden only by tests, which would otherwise move real megabytes. */
  chunkBytes?: number
}

/** Which delay this attempt waits: the three quick ones, then the steady beat. */
export function delayFor(attempt: number): number {
  return BACKOFF_MS[attempt] ?? STEADY_BACKOFF_MS
}

/**
 * `bytes 0-99/1000` → `{ first: 0, last: 99, total: 1000 }`; the 416 form
 * (`bytes`, a star, `/1000`) → the total only.
 */
export function parseContentRange(
  header: string | null,
): { first: number | null; last: number | null; total: number } | null {
  if (header === null) return null
  const span = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(header.trim())
  if (span !== null) {
    return { first: Number(span[1]), last: Number(span[2]), total: Number(span[3]) }
  }
  const unsatisfied = /^bytes \*\/(\d+)$/.exec(header.trim())
  if (unsatisfied !== null) return { first: null, last: null, total: Number(unsatisfied[1]) }
  return null
}

/** Whether a thrown write is the disk being full. */
export function isQuotaError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const name = (error as { name?: unknown }).name
  return name === 'QuotaExceededError' || name === 'NS_ERROR_DOM_QUOTA_REACHED'
}

/** Seconds from a `Retry-After` header, when it is a plain number. */
function retryAfterMs(response: Response): number {
  const raw = response.headers.get('Retry-After')
  if (raw === null || !/^\d+$/.test(raw.trim())) return 0
  return Number(raw.trim()) * 1000
}

/** Abandon a response without reading it (the §5.4a contract). */
function discard(response: Response, controller: AbortController): void {
  controller.abort()
  try {
    void response.body?.cancel().catch(() => undefined)
  } catch {
    // Already consumed, locked or aborted; nothing else to release.
  }
}

export async function runDownload(command: StartCommand, deps: DownloadDeps): Promise<void> {
  let handle: SyncHandle | null = null
  let terminal: TerminalMessage | null = null
  try {
    terminal = await download(command, deps, (opened) => {
      handle = opened
    })
  } catch (error) {
    terminal = {
      type: 'failed',
      name: command.name,
      offset: 0,
      code: isQuotaError(error) ? 'quota' : 'error',
      reason: String(error),
    }
  } finally {
    try {
      ;(handle as SyncHandle | null)?.close()
    } catch {
      // Already closed, or it never opened.
    }
    // Only now: the file is no longer held, whatever the manager does next.
    if (terminal !== null) deps.post(terminal)
  }
}

async function download(
  command: StartCommand,
  deps: DownloadDeps,
  opened: (handle: SyncHandle) => void,
): Promise<TerminalMessage> {
  const { name, url } = command
  let etag = command.etag
  let total = command.total
  let attempt = 0
  let restarts = 0
  const chunkBytes = deps.chunkBytes ?? CHUNK_BYTES

  const file = await deps.open(name)
  opened(file)
  if (command.fresh) {
    file.truncate(0)
    file.flush()
  }
  let offset = file.getSize()

  const failed = (code: FailCode, reason: string, at = offset): TerminalMessage => ({
    type: 'failed',
    name,
    offset: at,
    code,
    reason,
  })

  /** Start again from zero; a failure once that has happened too often in a row. */
  const restart = (reason: RestartReason, nextEtag: string | null): TerminalMessage | null => {
    restarts += 1
    file.truncate(0)
    file.flush()
    offset = 0
    total = null
    etag = nextEtag
    deps.post({ type: 'restarted', name, reason })
    if (restarts < MAX_RESTARTS) return null
    return failed('error', 'the server would not resume this download', 0)
  }

  const backoff = async (reason: string, atLeast = 0): Promise<void> => {
    attempt += 1
    const delay = Math.max(delayFor(attempt - 1), atLeast)
    deps.post({ type: 'retrying', name, attempt, delay, reason })
    await deps.sleep(delay)
  }

  // A file longer than the episode it is meant to be is not a resumable one.
  if (total !== null && offset > total) {
    const gaveUp = restart('total', null)
    if (gaveUp !== null) return gaveUp
  }
  // A file that is already whole (another account on this device kept the
  // same episode) is confirmed with one small request before it counts: the
  // server must still let *this* account have it, and still serve this encode.
  let verified = !(total !== null && offset >= total && offset > 0)

  while (!deps.stopped()) {
    const whole = total !== null && offset >= total
    if (whole && verified) break

    const first = whole && total !== null ? total - 1 : offset
    const last = whole && total !== null ? total - 1 : offset + chunkBytes - 1
    const headers: Record<string, string> = { Range: `bytes=${String(first)}-${String(last)}` }
    // With a known ETag the server answers 200 (whole file) instead of 206
    // when the rendition changed — which is how a re-encode is noticed.
    if (etag !== null) headers['If-Range'] = etag

    const controller = new AbortController()
    let response: Response
    try {
      response = await deps.fetch(url, {
        headers,
        credentials: 'include',
        signal: controller.signal,
      })
    } catch (error) {
      await backoff(String(error))
      continue
    }

    const status = response.status
    if (status === 401 || status === 403) {
      discard(response, controller)
      return failed(
        'auth',
        status === 401 ? 'you are signed out' : 'this account may not download episodes',
      )
    }
    if (status === 404) {
      discard(response, controller)
      return failed('gone', 'the server no longer has this episode')
    }
    if (status === 400) {
      discard(response, controller)
      return failed('error', 'the server refused the request (400)')
    }
    if (status === 416) {
      const range = parseContentRange(response.headers.get('Content-Range'))
      discard(response, controller)
      if (range !== null && range.total === offset && offset > 0) {
        total = range.total
        verified = true
        continue
      }
      const gaveUp = restart('range', null)
      if (gaveUp !== null) return gaveUp
      continue
    }
    if (status === 200) {
      // Never read: this is the whole of a file other than the one on disk.
      discard(response, controller)
      const gaveUp = restart('replaced', response.headers.get('ETag'))
      if (gaveUp !== null) return gaveUp
      continue
    }
    if (status !== 206) {
      discard(response, controller)
      await backoff(
        `the server answered ${String(status)}`,
        status === 429 ? retryAfterMs(response) : 0,
      )
      continue
    }

    const seen = response.headers.get('ETag')
    if (etag !== null && seen !== null && seen !== etag) {
      discard(response, controller)
      const gaveUp = restart('etag', seen)
      if (gaveUp !== null) return gaveUp
      continue
    }
    const range = parseContentRange(response.headers.get('Content-Range'))
    const declared = response.headers.get('Content-Length')
    const spanLength =
      range === null || range.last === null || range.first === null
        ? -1
        : range.last - range.first + 1
    const misfit =
      range === null ||
      range.first !== first ||
      range.last === null ||
      range.last > last ||
      spanLength <= 0 ||
      (declared !== null && Number(declared) !== spanLength)
    if (misfit) {
      // Never `arrayBuffer()` a body that is not the span asked for.
      discard(response, controller)
      const gaveUp = restart('range', seen ?? etag)
      if (gaveUp !== null) return gaveUp
      continue
    }
    if (total !== null && range.total !== total) {
      discard(response, controller)
      const gaveUp = restart('total', seen ?? etag)
      if (gaveUp !== null) return gaveUp
      continue
    }
    etag = seen ?? etag
    total = range.total

    if (whole) {
      // The one-byte check: authorised, and the same encode. Nothing to write.
      discard(response, controller)
      verified = true
      continue
    }

    let bytes: Uint8Array
    try {
      bytes = new Uint8Array(await response.arrayBuffer())
    } catch (error) {
      // The body died mid-flight: the same as a fetch that failed.
      await backoff(String(error))
      continue
    }
    if (bytes.byteLength === 0) {
      // A 206 with nothing in it would spin forever.
      return failed('error', 'the server sent an empty chunk')
    }
    if (bytes.byteLength > spanLength) {
      const gaveUp = restart('range', etag)
      if (gaveUp !== null) return gaveUp
      continue
    }

    try {
      file.write(bytes, { at: offset })
      file.flush()
    } catch (error) {
      // Whatever part of the chunk landed is cut off again, so the file's
      // size stays a chunk boundary the next run can resume from.
      try {
        file.truncate(offset)
        file.flush()
      } catch {
        // The disk is full enough that even this failed; the size check on
        // the next run catches what is left.
      }
      return isQuotaError(error)
        ? failed('quota', 'this device is out of space')
        : failed('error', String(error))
    }
    offset += bytes.byteLength
    attempt = 0
    restarts = 0
    deps.post({ type: 'progress', name, offset, total, etag })
  }

  const size = file.getSize()
  if (deps.stopped()) return { type: 'paused', name, offset: size }
  if (total === null || size !== total) {
    return failed(
      'size',
      `the file is ${String(size)} bytes but should be ${total === null ? 'a known size' : String(total)}`,
      size,
    )
  }
  return { type: 'done', name, offset: size, total, etag }
}
