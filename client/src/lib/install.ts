/**
 * Where Arc is running, as far as installing it is concerned (M18, spec FR-U1).
 *
 * Plain functions rather than hooks: none of these answers can change while a
 * page is open — a tab does not become an installed app under the viewer, it
 * is relaunched as one — so a subscription would be machinery for nothing.
 *
 * Ported from the sibling project Audiosey, which met each of these cases on
 * real devices first.
 */

/** Whether the page is already running as an installed, Home Screen app. */
export function isStandalone(): boolean {
  if (typeof window === 'undefined') return false
  // `navigator.standalone` is Apple's own flag and the one older iOS answers;
  // the media query is the standard one.
  const legacy = (navigator as Navigator & { standalone?: boolean }).standalone
  if (legacy === true) return true
  if (typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(display-mode: standalone)').matches
}

/**
 * Whether this is an iPhone or an iPad, whatever browser is on top.
 *
 * iPadOS asks for desktop sites by default and so reports itself as a Mac;
 * the tell is that no Mac has a touch screen, hence `maxTouchPoints`.
 */
export function isIos(): boolean {
  if (typeof navigator === 'undefined') return false
  const ua = navigator.userAgent
  const iPadAsMac = ua.includes('Macintosh') && navigator.maxTouchPoints > 1
  return /iPhone|iPod|iPad/.test(ua) || iPadAsMac
}

/**
 * Whether this is Chrome on an iPhone or iPad.
 *
 * It matters because Chrome's Share button is somewhere else: at the right of
 * the address bar rather than in Safari's toolbar. `CriOS` is Chrome's own
 * marker in the user agent, and the only one — underneath it is the same
 * WebKit as Safari.
 */
export function isChromeOnIos(): boolean {
  return isIos() && navigator.userAgent.includes('CriOS')
}

/**
 * Whether this is Safari on an iPhone or iPad. Firefox, Edge and Opera on iOS
 * are WebKit too but put their share action somewhere else again, so they are
 * deliberately not matched: a confident wrong instruction is worse than none.
 */
export function isIosSafari(): boolean {
  if (!isIos()) return false
  return !/CriOS|FxiOS|EdgiOS|OPiOS/.test(navigator.userAgent)
}

/** Which set of "Add to Home Screen" instructions this browser should get, if any. */
export type InstallGuide = 'safari' | 'chrome-ios' | null

/**
 * The install hint's one question: is there something true to tell this
 * viewer? Never once installed, never on a desktop (or Android, whose browsers
 * offer their own install prompt), and never in an iOS browser whose share
 * menu we cannot describe.
 */
export function installGuide(): InstallGuide {
  if (isStandalone()) return null
  if (isChromeOnIos()) return 'chrome-ios'
  if (isIosSafari()) return 'safari'
  return null
}
