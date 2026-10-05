import { useState } from 'react'
import { cx, FOCUS_RING } from '@/components/ui/styles'
import { installGuide, type InstallGuide } from '@/lib/install'

/**
 * "Add Arc to your Home Screen" — one quiet line above Watch Now, for an
 * iPad or iPhone that is still watching in a browser tab (M18, spec FR-U1).
 *
 * There is no install button to offer: iOS gives a page no
 * `beforeinstallprompt` and nothing it can call, so the install is two taps in
 * a menu the viewer has to be told about. Safari and Chrome put their Share
 * button in different places, so the two get different words; any other iOS
 * browser, any desktop, and the installed app itself get nothing at all
 * (`lib/install.ts`).
 *
 * Built like the demo account's first-visit strip on Watch Now: one line, a ✕,
 * and once dismissed it does not come back in this browser. The key is not
 * per user — installing is something a device does, not an account.
 */
export const INSTALL_HINT_KEY = 'arc:install-hint-dismissed'

function hintDismissed(): boolean {
  try {
    return window.localStorage.getItem(INSTALL_HINT_KEY) === '1'
  } catch {
    // Storage blocked: show the hint, which is the harmless half of failing.
    return false
  }
}

function rememberDismissed(): void {
  try {
    window.localStorage.setItem(INSTALL_HINT_KEY, '1')
  } catch {
    // It comes back next visit; nothing worse.
  }
}

/** Apple's share glyph — a tray with an arrow out of it — drawn inline. */
function ShareGlyph() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 16 16"
      className="mx-0.5 inline h-[15px] w-[15px] -translate-y-px fill-none stroke-current align-middle"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 1.5v8.5M5 4.5l3-3 3 3M5.5 7H4v7.5h8V7h-1.5" />
    </svg>
  )
}

function Instructions({ guide }: { guide: Exclude<InstallGuide, null> }) {
  const strong = 'font-semibold text-[var(--arc-text)]'
  return guide === 'chrome-ios' ? (
    <>
      Tap Share <ShareGlyph /> at the right of Chrome’s address bar, then{' '}
      <span className={strong}>Add to Home Screen</span>.
    </>
  ) : (
    <>
      Tap Share <ShareGlyph /> in Safari, then <span className={strong}>Add to Home Screen</span>.
    </>
  )
}

export function InstallHint() {
  // Read once: neither answer changes while the page is open.
  const [guide] = useState(installGuide)
  const [dismissed, setDismissed] = useState(hintDismissed)

  if (guide === null || dismissed) return null

  return (
    <div
      role="note"
      aria-label="Install Arc"
      className="mb-5 flex items-center gap-3 rounded-nav border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] py-1 pr-1 pl-3.5"
    >
      <p className="min-w-0 flex-1 py-1.5 text-[14px] text-[var(--arc-text-muted)]">
        <span className="font-semibold text-[var(--arc-text)]">Watch from your Home Screen.</span>{' '}
        <Instructions guide={guide} />
      </p>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => {
          rememberDismissed()
          setDismissed(true)
        }}
        className={cx(
          'flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-[14px] leading-none text-[var(--arc-text-muted)] hover:text-[var(--arc-text)]',
          FOCUS_RING,
        )}
      >
        <span aria-hidden>✕</span>
      </button>
    </div>
  )
}
