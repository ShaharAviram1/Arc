import { afterEach, describe, expect, it } from 'vitest'
import { altNameFor, EPISODE_FILE, fileSize, slotsFor } from '@/offline/opfs'

/** What a size read can say (M20.4 E): a size, missing, or "could not ask". */

function storage(getDirectory: () => Promise<unknown>): void {
  Object.defineProperty(navigator, 'storage', { configurable: true, value: { getDirectory } })
}

function directory(getFileHandle: (name: string) => Promise<unknown>) {
  return () => Promise.resolve({ getFileHandle })
}

afterEach(() => {
  Reflect.deleteProperty(navigator, 'storage')
})

describe('fileSize', () => {
  it('answers the size of a file that is there', async () => {
    storage(
      directory(() => Promise.resolve({ getFile: () => Promise.resolve(new File(['abc'], 'x')) })),
    )
    expect(await fileSize('episode-1.mp4')).toBe(3)
  })

  it('answers missing only when the browser says NotFoundError', async () => {
    storage(directory(() => Promise.reject(new DOMException('no', 'NotFoundError'))))
    expect(await fileSize('episode-1.mp4')).toBe('missing')
  })

  it('answers unreadable for any other failure, never a zero', async () => {
    storage(directory(() => Promise.reject(new DOMException('busy', 'InvalidStateError'))))
    expect(await fileSize('episode-1.mp4')).toBe('unreadable')

    storage(
      directory(() =>
        Promise.resolve({
          getFile: () => Promise.reject(new DOMException('x', 'NotReadableError')),
        }),
      ),
    )
    expect(await fileSize('episode-1.mp4')).toBe('unreadable')

    storage(() => Promise.reject(new Error('no directory')))
    expect(await fileSize('episode-1.mp4')).toBe('unreadable')

    Reflect.deleteProperty(navigator, 'storage')
    expect(await fileSize('episode-1.mp4')).toBe('unreadable')
  })
})

describe('altNameFor', () => {
  it('alternates a copy between its two slots, both swept by name', () => {
    expect(altNameFor('episode-9-o.mp4')).toBe('episode-9-o-b.mp4')
    expect(altNameFor('episode-9-o-b.mp4')).toBe('episode-9-o.mp4')
    expect(altNameFor('episode-9-o-c.mp4')).toBe('episode-9-o.mp4')
    expect(slotsFor('episode-9-o-c.mp4')).toEqual([
      'episode-9-o.mp4',
      'episode-9-o-b.mp4',
      'episode-9-o-c.mp4',
    ])
    expect(altNameFor('episode-9.mp4')).toBe('episode-9-b.mp4')
    for (const name of ['episode-9-o.mp4', 'episode-9-o-b.mp4', 'episode-9-o-c.mp4']) {
      expect(EPISODE_FILE.test(name)).toBe(true)
    }
    expect(EPISODE_FILE.test('episode-9-x.mp4')).toBe(false)
  })
})
