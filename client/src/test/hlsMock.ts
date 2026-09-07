/**
 * A stand-in for hls.js, for `vi.mock('hls.js', () => import('@/test/hlsMock'))`.
 *
 * jsdom has no Media Source Extensions, so the real library refuses to run
 * there — and even if it did, a test has no business pulling in a megabyte of
 * demuxer. What the player actually needs from it is small and worth
 * asserting: the credentials hook, the playlist it was handed, the element it
 * attached to, and that the instance is destroyed. Each fake records exactly
 * those, and `emit` lets a test push an error through the same handler the
 * real library would.
 */

import { vi } from 'vitest'

export interface FakeHlsConfig {
  xhrSetup?: (xhr: XMLHttpRequest, url: string) => void
}

/** Only the event the player subscribes to. */
export const Events = { ERROR: 'hlsError' } as const

type Handler = (event: string, data: unknown) => void

/** Every instance the component has constructed, in order. */
export const instances: FakeHls[] = []

export class FakeHls {
  static readonly Events = Events
  static readonly isSupported = vi.fn(() => true)

  readonly config: FakeHlsConfig
  readonly loadSource = vi.fn<(src: string) => void>()
  readonly attachMedia = vi.fn<(media: HTMLMediaElement) => void>()
  readonly destroy = vi.fn<() => void>()

  private readonly handlers = new Map<string, Handler[]>()

  constructor(config: FakeHlsConfig = {}) {
    this.config = config
    instances.push(this)
  }

  on(event: string, handler: Handler): void {
    const existing = this.handlers.get(event) ?? []
    existing.push(handler)
    this.handlers.set(event, existing)
  }

  emit(event: string, data: unknown): void {
    for (const handler of this.handlers.get(event) ?? []) handler(event, data)
  }
}

/** Call from `beforeEach`: the instance list and `isSupported` are module state. */
export function resetHls(): void {
  instances.length = 0
  FakeHls.isSupported.mockReturnValue(true)
}

export default FakeHls
