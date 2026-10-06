/// <reference lib="webworker" />
/**
 * The download worker: the browser half of `download.ts` (spec FR-S9).
 *
 * It exists as a worker for one reason — a **sync access handle** on an OPFS
 * file is available only inside a worker.
 *
 * Everything with a decision in it lives in `download.ts`. This file supplies
 * the real dependencies and owns the worker's own state: which file it is
 * writing, whether a pause has been asked for, what to start next, and how to
 * cut a backoff short when the app comes back on screen.
 *
 * One file at a time. A `download` for the running file is the manager's
 * nudge ("on screen again / online again") and ends any backoff — unless a
 * pause for it is already under way, in which case it runs again *after* the
 * file is closed, never by un-pausing a loop the manager has given up on. A
 * `download` for a different file stops the current one at its next chunk
 * boundary and runs after it.
 *
 * **One window per file.** Each run holds a Web Lock named after the file
 * (`navigator.locks`, where there is one). A second Arc window asking for the
 * same file gets `busy` back instead of a second writer on one file. The name
 * carries the copy (`episode-<id>.mp4` / `episode-<id>-o.mp4`), so the full
 * and the small copy of one episode never share a lock or a file.
 */

import {
  runDownload,
  type StartCommand,
  type SyncHandle,
  type WorkerCommand,
  type WorkerMessage,
} from './download'

declare const self: DedicatedWorkerGlobalScope

let stopped = false
let current: string | null = null
let next: StartCommand | null = null
/** Set while the loop is asleep in a backoff, so a nudge can end it early. */
let wake: (() => void) | null = null

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(finish, ms)
    wake = finish
    function finish() {
      clearTimeout(timer)
      wake = null
      resolve()
    }
  })
}

async function open(name: string): Promise<SyncHandle> {
  const root = await navigator.storage.getDirectory()
  const file = await root.getFileHandle(name, { create: true })
  return await file.createSyncAccessHandle()
}

function post(message: WorkerMessage): void {
  self.postMessage(message)
}

function run(command: StartCommand): Promise<void> {
  return runDownload(command, {
    fetch: (url, init) => fetch(url, init),
    open,
    post: (message) => {
      post({ ...message, run: command.run })
    },
    sleep,
    stopped: () => stopped,
  })
}

/** The run, under the file's lock when the browser has Web Locks. */
async function locked(command: StartCommand): Promise<void> {
  const locks = (navigator as WorkerNavigator & { locks?: LockManager }).locks
  if (locks === undefined) {
    await run(command)
    return
  }
  await locks.request(`arc-download:${command.name}`, { ifAvailable: true }, async (lock) => {
    if (lock === null) {
      post({ type: 'busy', name: command.name, run: command.run })
      return
    }
    await run(command)
  })
}

function start(command: StartCommand): void {
  stopped = false
  current = command.name
  void locked(command)
    .catch((error: unknown) => {
      post({
        type: 'failed',
        name: command.name,
        offset: 0,
        code: 'error',
        reason: String(error),
        run: command.run,
      })
    })
    .finally(() => {
      current = null
      const following = next
      next = null
      if (following !== null) start(following)
    })
}

self.onmessage = (event: MessageEvent<WorkerCommand>) => {
  const command = event.data
  if (command.cmd === 'pause') {
    if (next?.name === command.name) next = null
    if (current === command.name) {
      stopped = true
      wake?.()
    }
    return
  }
  if (current === null) {
    start(command)
    return
  }
  if (current === command.name && !stopped) {
    wake?.()
    return
  }
  next = command
  stopped = true
  wake?.()
}

export {}
