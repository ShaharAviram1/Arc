import { describe, expect, it } from 'vitest'
import { isShellNavigation, pwaOptions } from '@/lib/pwa'

/**
 * The service worker's one rule (M18; architecture.md §3, "Installable web
 * app"): the app shell and nothing else. It must never answer or cache
 * anything under `/api/*` or `/media/*`; those fetches go straight to the
 * network as if there were no worker.
 */

type Ctx = Parameters<typeof isShellNavigation>[0]

function ctx(path: string, mode = 'navigate'): Ctx {
  return { request: { mode }, url: new URL(path, 'https://arc.example') }
}

/**
 * What Workbox does with the predicate: it writes `fn.toString()` into
 * `sw.js`. Rebuilding it from its source text proves it refers to nothing
 * outside its own body — an imported helper would be a ReferenceError there.
 */
function asSerialised(): typeof isShellNavigation {
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  const rebuild = new Function(
    `return (${isShellNavigation.toString()})`,
  ) as () => typeof isShellNavigation
  return rebuild()
}

const PAGES = ['/', '/schedule', '/anime/42', '/watch/7', '/invite/abc', '/apiary', '/mediathek']
const OFF_LIMITS = [
  '/api',
  '/api/',
  '/api/auth/me',
  '/api/events',
  '/api/mal/callback?code=x&state=y',
  '/media',
  '/media/episodes/12/master.m3u8',
  '/media/episodes/12/seg-0003.m4s',
]

describe('isShellNavigation', () => {
  for (const predicate of [isShellNavigation, asSerialised()]) {
    it.each(PAGES)('answers a navigation to %s', (path) => {
      expect(predicate(ctx(path))).toBe(true)
    })

    it.each(OFF_LIMITS)('never answers %s, even as a navigation', (path) => {
      expect(predicate(ctx(path))).toBe(false)
      expect(predicate(ctx(path, 'cors'))).toBe(false)
      expect(predicate(ctx(path, 'same-origin'))).toBe(false)
    })

    it('leaves every non-navigation request alone', () => {
      expect(predicate(ctx('/assets/index-abc.js', 'no-cors'))).toBe(false)
      expect(predicate(ctx('/', 'cors'))).toBe(false)
    })
  }
})

describe('pwaOptions', () => {
  const workbox = pwaOptions.workbox ?? {}

  it('has exactly one runtime route, and it is the shell navigation', () => {
    expect(workbox.runtimeCaching).toHaveLength(1)
    const [route] = workbox.runtimeCaching ?? []
    expect(route?.urlPattern).toBe(isShellNavigation)
    expect(route?.handler).toBe('NetworkFirst')
    // Nothing a navigation returns is ever stored: 599 is never a real status.
    expect(route?.options?.cacheableResponse).toEqual({ statuses: [599] })
    expect(route?.options?.networkTimeoutSeconds).toBe(3)
  })

  it('has no catch-all navigation fallback and no directory index', () => {
    expect(workbox.navigateFallback).toBeNull()
    expect(workbox.directoryIndex).toBeNull()
  })

  it('precaches only the built shell', () => {
    expect(workbox.globPatterns).toEqual(['**/*.{js,css,html,svg,png,woff2}'])
    expect(workbox.additionalManifestEntries).toBeUndefined()
  })

  it('updates on the next launch and never reloads the page under the viewer', () => {
    expect(pwaOptions.registerType).toBe('prompt')
    expect(workbox.skipWaiting).toBeUndefined()
    expect(workbox.clientsClaim).toBeUndefined()
  })

  it('is not registered under vite dev', () => {
    expect(pwaOptions.devOptions?.enabled).toBe(false)
  })

  it('installs as a standalone app that turns with the device', () => {
    const manifest = pwaOptions.manifest || undefined
    expect(manifest).toMatchObject({
      name: 'Arc',
      short_name: 'Arc',
      display: 'standalone',
      start_url: '/',
      scope: '/',
      theme_color: '#080b11',
      background_color: '#080b11',
    })
    expect(manifest).not.toHaveProperty('orientation')
  })
})
