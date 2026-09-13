import { useCallback, useSyncExternalStore } from 'react'

/**
 * What shape an image is, before the frame that has to draw it renders.
 *
 * Two frames pick their treatment from the ratio of a picture they have not
 * loaded yet — the 21:9 hero (`HeroFrame`) and the 16:9 episode card on Watch
 * Now — and rendering the picture to find out is the zoomed strip the rule
 * exists to prevent. So the ratio is measured off-frame (`AspectProbe`) and
 * the wash holds the frame until it is known.
 *
 * Which was fine for one frame and wrong for a carousel: Watch Now cycles six
 * shows through one frame every eight seconds, the ratio lived in that frame's
 * `useState`, and every rotation therefore started at "not known yet" — the
 * blurred wash — and swapped to the backdrop a moment later. The owner saw it
 * as "semi-transparent posters" flashing on every slide (Safari, production,
 * 2026-09-13), on a season where every show has a backdrop.
 *
 * So shape is known in three ways, in this order, and only the third waits:
 *
 * 1. **Trusted.** A TMDB backdrop is 16:9 by construction. The catalogue says
 *    which art came from `backdrop_url` (`heroArt` in `lib/anime`, which
 *    carries the ratio beside the url), and `trustedAspect` recognises TMDB's
 *    own CDN for the callers that pass a bare url.
 * 2. **Remembered.** Every measurement any probe has ever made, for the life
 *    of the tab: a url measured for slide 4 is still known when slide 4 comes
 *    round again, and when the show page draws the same art a minute later.
 * 3. **Measured.** A probe, and the wash until it answers — an AniList banner
 *    is a ~4.75:1 strip and nothing but the browser can say so.
 *
 * The store is module-level because it is a fact about a url, not about a
 * component: two frames and a page each hold a piece of the same question.
 */

/**
 * 16:9. The shape of a TMDB backdrop — see `heroArt`, which carries this from
 * the column the art came from rather than from the url it happens to have.
 */
const BACKDROP_ASPECT = 16 / 9

/** Measured shapes, by url. Grows with the art a session has actually seen. */
const measured = new Map<string, number>()

/** `useKnownAspect` subscribers: a frame waiting on a shape it cannot see. */
const listeners = new Set<() => void>()

/**
 * The shape a url is known to be from the url alone, or null.
 *
 * TMDB serves every backdrop from `image.tmdb.org/t/p/<size>/…` and they are
 * all 16:9, which fills a 21:9 frame at the cost of a sliver off the top and
 * bottom. Nothing else is trusted: AniList's CDN serves the 4.75:1 banner and
 * the 2:3 cover from the same host and path.
 *
 * A caller that knows which column the art came from should say so instead —
 * this is the belt for the pages that pass a bare url.
 */
export function trustedAspect(url: string | null): number | null {
  if (url === null || url === '') return null
  let host: string
  let path: string
  try {
    const parsed = new URL(url)
    host = parsed.hostname
    path = parsed.pathname
  } catch {
    // A relative url, which is never TMDB's CDN.
    return null
  }
  return host === 'image.tmdb.org' && path.startsWith('/t/p/') ? BACKDROP_ASPECT : null
}

/** What is known about a url's shape right now, without loading anything. */
export function knownAspect(url: string | null): number | null {
  if (url === null || url === '') return null
  return trustedAspect(url) ?? measured.get(url) ?? null
}

/**
 * Records a measurement and wakes the frames waiting on it.
 *
 * The same number twice is dropped rather than announced: a ref callback runs
 * on every render and reports the size of an image that is already loaded, so
 * a store that notified unconditionally would be a render loop.
 */
export function rememberAspect(url: string, aspect: number): void {
  if (url === '' || !Number.isFinite(aspect) || aspect <= 0) return
  if (measured.get(url) === aspect) return
  measured.set(url, aspect)
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/**
 * {@link knownAspect}, re-rendering when a probe anywhere answers for `url`.
 *
 * Synchronous on the first render, which is the whole point: a shape that is
 * already known never gets a frame of the wash.
 */
export function useKnownAspect(url: string | null): number | null {
  const snapshot = useCallback(() => knownAspect(url), [url])
  return useSyncExternalStore(subscribe, snapshot, snapshot)
}

/**
 * Empties the remembered shapes. **Tests only**: the store outlives a render
 * tree by design, so a suite that measures the same url twice — once as a
 * backdrop, once as a strip — has to start each test from nothing. Called
 * from `src/test/setup.ts` before every test, between renders, so it does not
 * notify anybody.
 */
export function clearAspectCache(): void {
  measured.clear()
}
