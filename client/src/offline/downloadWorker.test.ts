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

  it('hashes a held file beside the download loop, and says when it is missing', async () => {
    const bytes = new TextEncoder().encode('abc')
    Object.defineProperty(navigator, 'storage', {
      configurable: true,
      value: {
        getDirectory: () =>
          Promise.resolve({
            getFileHandle: (name: string) =>
              name === 'episode-5.mp4'
                ? Promise.resolve({ getFile: () => Promise.resolve(new File([bytes], name)) })
                : Promise.reject(new DOMException('no', 'NotFoundError')),
          }),
      },
    })

    send({ cmd: 'hash', name: 'episode-5.mp4', ask: 1 })
    send({ cmd: 'hash', name: 'episode-6.mp4', ask: 2 })

    await vi.waitFor(() => {
      expect(posted).toHaveLength(2)
    })
    expect(posted).toContainEqual({
      type: 'hashed',
      name: 'episode-5.mp4',
      ask: 1,
      size: 3,
      sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    })
    expect(posted).toContainEqual({
      type: 'hashed',
      name: 'episode-6.mp4',
      ask: 2,
      size: 0,
      sha256: null,
    })
  })
})
