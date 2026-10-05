import { vi } from 'vitest'

/**
 * Real user-agent strings for the devices the install hint has to tell apart
 * (M18). iPadOS asks for desktop sites by default, so its Safari is the Mac
 * string — only the touch points give it away.
 */
export const UA = {
  ipadSafari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
  macSafari:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
  iphoneSafari:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  ipadChrome:
    'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/129.0.6668.69 Mobile/15E148 Safari/604.1',
  iphoneFirefox:
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/131.0 Mobile/15E148 Safari/605.1.15',
  windowsChrome:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
} as const

interface Device {
  ua: string
  touchPoints?: number
  /** Running from the Home Screen: both of the ways a browser can say so. */
  standalone?: boolean
}

const STUBBED = ['userAgent', 'maxTouchPoints', 'standalone'] as const

/**
 * Pretends to be a device. Own properties on `navigator` shadow jsdom's
 * prototype getters; {@link resetDevice} deletes them again. `matchMedia` is
 * stubbed through `vi.stubGlobal`, so `vi.unstubAllGlobals` restores it.
 */
export function stubDevice({ ua, touchPoints = 0, standalone = false }: Device): void {
  const values = { userAgent: ua, maxTouchPoints: touchPoints, standalone }
  for (const key of STUBBED) {
    Object.defineProperty(navigator, key, { value: values[key], configurable: true })
  }
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: standalone && query === '(display-mode: standalone)',
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }))
}

export function resetDevice(): void {
  for (const key of STUBBED) {
    Reflect.deleteProperty(navigator, key)
  }
  vi.unstubAllGlobals()
}
