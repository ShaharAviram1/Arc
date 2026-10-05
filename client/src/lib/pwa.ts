/**
 * The service worker's configuration (M18; architecture.md §3, "Installable
 * web app"): the app shell, and nothing else.
 *
 * It lives here rather than inline in `vite.config.ts` so that a unit test can
 * hold it to its one rule (`pwa.test.ts`): **the worker never intercepts,
 * caches or answers `/api/*` or `/media/*`.** Not the session-bound JSON, not
 * the live event stream at `/api/events`, not the MyAnimeList OAuth callback
 * the browser navigates to, and not a byte of video. API responses held by a
 * service worker are a second copy of the server's state that disagrees with
 * it in ways nobody can explain; a ranged HLS segment answered by a worker is
 * slower than one answered by the network, and every one of those responses is
 * per-user authorised (`Cache-Control: private`) and must never sit in a
 * shared cache.
 *
 * Type-only imports: this module is read by `vite.config.ts` at build time and
 * by Vitest, and never by the app bundle.
 */

import type { VitePWAOptions } from 'vite-plugin-pwa'

/** The shape Workbox hands a route's `urlPattern` callback, as far as we read it. */
interface RouteContext {
  request: { mode: string }
  url: { pathname: string }
}

/**
 * Whether a request is a page navigation into the client — the only thing the
 * worker's runtime route may answer.
 *
 * Self-contained on purpose: Workbox serialises this function into `sw.js`
 * with `Function.prototype.toString`, so it may not refer to anything outside
 * its own body (no imported helper, no module constant).
 */
export const isShellNavigation = ({ request, url }: RouteContext): boolean =>
  request.mode === 'navigate' && !/^\/(api|media)(\/|$)/.test(url.pathname)

export const pwaOptions: Partial<VitePWAOptions> = {
  // `prompt` with no prompt UI: a new worker installs in the background and
  // waits, and takes over the next time the app is launched. `autoUpdate`
  // would reload the page under the viewer — mid-episode, which is the worst
  // moment Arc has. Because navigations are network-first (below), an online
  // launch already shows the new build; what lands on the *second* launch is
  // the new worker, i.e. the new offline shell.
  registerType: 'prompt',
  injectRegister: 'auto',
  workbox: {
    globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
    // Navigations go to the NETWORK first, and fall back to the precached
    // shell only when the network cannot answer within three seconds (no
    // connection, a captive portal, a stalled tunnel). A plain
    // `navigateFallback` answers every navigation from the precache, so the
    // first load after each deploy is the previous build — the sibling project
    // this configuration comes from hit that three times in two days (an
    // install that captured stale meta tags, an update that needed two
    // launches, a link opened by a build that did not have its route yet).
    //
    // The network response is deliberately never stored (no status is
    // cacheable): a runtime copy of a newer index.html names hashed assets
    // the active worker has not precached, and would break the offline
    // fallback. Workbox only offers a network timeout on `NetworkFirst`, hence
    // that handler with an empty cache.
    navigateFallback: null,
    // And `/` must not be answered by the precache either: with the default
    // `directoryIndex`, a navigation to `/` (the installed app's start_url)
    // matches the precached `index.html` before the route below is consulted,
    // which is the stale-shell bug again on the one URL every launch opens.
    directoryIndex: null,
    runtimeCaching: [
      {
        urlPattern: isShellNavigation,
        handler: 'NetworkFirst',
        options: {
          cacheName: 'navigations-never-stored',
          networkTimeoutSeconds: 3,
          cacheableResponse: { statuses: [599] },
          precacheFallback: { fallbackURL: 'index.html' },
        },
      },
    ],
    // The shell is a few hundred kilobytes; this only guards against a stray
    // large asset silently bloating every install.
    maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
    cleanupOutdatedCaches: true,
  },
  manifest: {
    name: 'Arc',
    short_name: 'Arc',
    description: 'Your anime, ready to watch.',
    id: '/',
    start_url: '/',
    scope: '/',
    display: 'standalone',
    // No `orientation`: episodes are watched in landscape and browsed in
    // either, so the installed app turns with the device.
    background_color: '#080b11',
    theme_color: '#080b11',
    lang: 'en',
    dir: 'ltr',
    // Opaque on #080b11 (scripts/make-pwa-icons.sh): iOS paints transparency
    // black or white and adds its own corner mask.
    icons: [
      { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
      { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
      {
        src: 'pwa-maskable-512x512.png',
        sizes: '512x512',
        type: 'image/png',
        purpose: 'maskable',
      },
    ],
  },
  devOptions: {
    // Off under `vite dev`: a service worker in front of a hot-reloading dev
    // server only ever answers "why is my change not showing".
    enabled: false,
  },
}
