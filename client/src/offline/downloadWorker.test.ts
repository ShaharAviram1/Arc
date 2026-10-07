import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { WorkerCommand, WorkerMessage } from '@/offline/download'

/**
 * The worker's own bookkeeping: a download waiting its turn behind the running
 * one that is paused or replaced before it opens its file still gets a
 * terminal message, so the manager stops counting the file as held.
 */

const posted: WorkerMessage[] = []

function send(command: WorkerCommand): void {
  const handler = self.onmessage as ((event: MessageEvent<WorkerCommand>) => void) | null
  handler?.(new MessageEvent('message', { data: command }))
}

function download(name: string, run: number): WorkerCommand {
  return {
    cmd: 'download',
    name,
    url: `/media/${name}`,
    etag: null,
    total: null,
    fresh: false,
    run,
  }
}

beforeEach(async () => {
  posted.length = 0
  vi.resetModules()
  // The running download never gets its file: it stays the current one.
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: { getDirectory: () => new Promise(() => undefined) },
  })
  vi.spyOn(self, 'postMessage').mockImplementation((message: unknown) => {
    posted.push(message as WorkerMessage)
  })
  await import('@/offline/downloadWorker')
})

afterEach(() => {
  vi.restoreAllMocks()
  Reflect.deleteProperty(navigator, 'storage')
  self.onmessage = null
})

describe('the download worker', () => {
  it('says it released a waiting download that was paused before it opened', () => {
    send(download('episode-1.mp4', 1))
    send(download('episode-2.mp4', 2))

    send({ cmd: 'pause', name: 'episode-2.mp4' })

    expect(posted).toEqual([{ type: 'released', name: 'episode-2.mp4', run: 2 }])
  })

  it('says it released a waiting download that another one replaced', () => {
    send(download('episode-1.mp4', 1))
    send(download('episode-2.mp4', 2))
    send(download('episode-3.mp4', 3))

    expect(posted).toEqual([{ type: 'released', name: 'episode-2.mp4', run: 2 }])
  })

  it('leaves a waiting rerun of the running file to that run’s own terminal message', () => {
    send(download('episode-1.mp4', 1))
    send({ cmd: 'pause', name: 'episode-1.mp4' })
    send(download('episode-1.mp4', 2))

    send({ cmd: 'pause', name: 'episode-1.mp4' })

    expect(posted).toEqual([])
  })
})
