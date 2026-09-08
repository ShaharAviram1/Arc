import { vi } from 'vitest'

/**
 * A stubbed `window.location.assign`.
 *
 * jsdom's own `assign` is non-configurable — `vi.spyOn` throws on it — and
 * calling it for real would navigate the test document away. Replacing the
 * whole `location` object keeps every property tests read (`href`, `search`)
 * and swaps only the navigation, and `vi.unstubAllGlobals()` in an `afterEach`
 * puts the real one back.
 */
export function stubLocationAssign(): ReturnType<typeof vi.fn<(url: string) => void>> {
  const assign = vi.fn<(url: string) => void>()
  vi.stubGlobal('location', { ...window.location, assign })
  return assign
}
