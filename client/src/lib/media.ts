/**
 * Viewport questions the shell has to answer in JavaScript.
 *
 * Almost all of the responsive work in Arc is CSS, and should stay that way.
 * The phone chrome is the exception: the bottom tab bar and the "More" sheet
 * are not a restyled toolbar, they are different elements with different
 * behaviour, and rendering both and hiding one would put a second `<nav>`,
 * a second search field and a duplicate set of account links into the
 * accessibility tree on every desktop page.
 *
 * Reduced motion is the second exception. `index.css` already turns every CSS
 * animation and transition off under `prefers-reduced-motion`, but a hero that
 * advances itself on a timer is motion no stylesheet can reach: the component
 * has to stop starting the timer.
 */

import { useCallback, useSyncExternalStore } from 'react'

/** Below this the shell is the phone one. Matches Tailwind's `md` breakpoint. */
export const PHONE_MEDIA_QUERY = '(max-width: 767px)'

/** The OS-level "stop animating things at me" setting. */
export const REDUCED_MOTION_MEDIA_QUERY = '(prefers-reduced-motion: reduce)'

function listFor(query: string): MediaQueryList | null {
  // Guarded because a test environment may not implement it, and a shell that
  // throws is worse than one that assumes the desktop layout.
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  return window.matchMedia(query)
}

/** Subscribes to one media query. False on the server and where it is absent. */
function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = listFor(query)
      if (list === null || typeof list.addEventListener !== 'function') return () => undefined
      list.addEventListener('change', onChange)
      return () => {
        list.removeEventListener('change', onChange)
      }
    },
    [query],
  )
  const snapshot = useCallback(() => listFor(query)?.matches ?? false, [query])

  return useSyncExternalStore(subscribe, snapshot, () => false)
}

/** True on phone widths. False on the server and anywhere matchMedia is absent. */
export function useIsPhone(): boolean {
  return useMediaQuery(PHONE_MEDIA_QUERY)
}

/** True when the viewer has asked the OS for less movement. */
export function usePrefersReducedMotion(): boolean {
  return useMediaQuery(REDUCED_MOTION_MEDIA_QUERY)
}
