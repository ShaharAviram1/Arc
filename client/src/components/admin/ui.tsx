/**
 * The handful of pieces every admin tab is built from.
 *
 * The admin page is five tables and four forms, and without something like
 * this each tab grows its own button, its own "that failed" line and its own
 * idea of what a confirmation looks like. Nothing here is clever: class
 * strings so the tabs agree with each other and with the rest of Arc, plus the
 * two behaviours that would otherwise be copied five times — a destructive
 * button that asks first, and a table that scrolls instead of pushing the page
 * sideways on a narrow screen.
 *
 * The design language is the one the other pages already speak (`ErrorState`,
 * `Recs`): surfaces on `--arc-surface`, muted secondary text, accent for the
 * one primary action, `--arc-error` for anything that removes something. M15's
 * design pass will take all of it somewhere better; until then, consistency is
 * worth more than invention.
 */

import { useState, type ReactNode } from 'react'

export const primaryButtonClass =
  'inline-flex items-center gap-2 rounded-md bg-[var(--arc-accent)] px-3 py-1.5 text-sm font-medium text-[var(--arc-accent-contrast)] transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

export const subtleButtonClass =
  'inline-flex items-center gap-1.5 rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-2.5 py-1 text-xs text-[var(--arc-text)] transition-colors hover:border-[var(--arc-accent)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

export const dangerButtonClass =
  'inline-flex items-center gap-1.5 rounded-md border border-[var(--arc-error)]/40 bg-[var(--arc-error)]/10 px-2.5 py-1 text-xs text-[var(--arc-error)] transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-error)] disabled:cursor-not-allowed disabled:opacity-60'

export const inputClass =
  'rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-3 py-1.5 text-sm text-[var(--arc-text)] placeholder:text-[var(--arc-text-muted)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)] disabled:opacity-60'

export const panelClass = 'rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-4'

export const thClass =
  'px-3 py-2 text-left text-xs font-medium tracking-wide text-[var(--arc-text-muted)] uppercase'

export const tdClass = 'px-3 py-2 align-top text-sm text-[var(--arc-text)]'

/** A section heading, so every tab's headings are the same size and weight. */
export function SectionHeading({ children }: { children: ReactNode }) {
  return <h2 className="text-lg font-semibold tracking-tight text-[var(--arc-text)]">{children}</h2>
}

/**
 * A table that scrolls inside itself.
 *
 * Every admin table has more columns than a phone has room for, and a page
 * that scrolls sideways as a whole is the one outcome worse than a table that
 * does. `min-w-full` on the table inside keeps it filling the space when there
 * is room.
 */
export function TableScroll({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)]">
      {children}
    </div>
  )
}

export type Tone = 'ok' | 'warn' | 'bad' | 'muted' | 'busy'

const TONE_CLASSES: Record<Tone, string> = {
  ok: 'text-[var(--arc-ok)] border-[var(--arc-ok)]/40 bg-[var(--arc-ok)]/10',
  warn: 'text-[var(--arc-warn)] border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10',
  bad: 'text-[var(--arc-error)] border-[var(--arc-error)]/40 bg-[var(--arc-error)]/10',
  busy: 'text-[var(--arc-accent)] border-[var(--arc-accent)]/40 bg-[var(--arc-accent)]/10',
  muted: 'text-[var(--arc-text-muted)] border-[var(--arc-border)] bg-[var(--arc-surface-raised)]',
}

/** A small coloured label: a status, a count, a state. */
export function Pill({ tone = 'muted', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs whitespace-nowrap ${TONE_CLASSES[tone]}`}
    >
      {children}
    </span>
  )
}

/** One failed action, said next to the control that caused it. */
export function InlineError({ message, className = '' }: { message: string; className?: string }) {
  return (
    <p role="alert" className={`text-sm text-[var(--arc-error)] ${className}`}>
      {message}
    </p>
  )
}

/** One thing that worked, said the same way in every tab. */
export function Notice({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <p role="status" className={`text-sm text-[var(--arc-ok)] ${className}`}>
      {children}
    </p>
  )
}

export interface ConfirmButtonProps {
  /** The button as it reads before anything is armed, e.g. "Deactivate". */
  label: string
  /** The question, e.g. "Deactivate leah@example.com?" */
  question: string
  /** The button that does it, e.g. "Yes, deactivate". */
  confirmLabel: string
  onConfirm: () => void
  /** True while the confirmed action is in flight; the button goes quiet. */
  pending?: boolean
}

/**
 * A destructive action that asks first, in place.
 *
 * In place rather than in a dialog because the row is the context: "Deactivate
 * leah@example.com?" beside Leah's row needs no more explaining, and a modal
 * over a table is a heavier thing to build, to make accessible, and to escape
 * from than this page warrants. Arming is local state, so two rows can never
 * be armed by accident and navigating away disarms everything.
 */
export function ConfirmButton({
  label,
  question,
  confirmLabel,
  onConfirm,
  pending = false,
}: ConfirmButtonProps) {
  const [armed, setArmed] = useState(false)

  if (!armed) {
    return (
      <button
        type="button"
        className={dangerButtonClass}
        onClick={() => {
          setArmed(true)
        }}
      >
        {label}
      </button>
    )
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span className="text-xs text-[var(--arc-text-muted)]">{question}</span>
      <button
        type="button"
        className={dangerButtonClass}
        disabled={pending}
        onClick={() => {
          setArmed(false)
          onConfirm()
        }}
      >
        {confirmLabel}
      </button>
      <button
        type="button"
        className={subtleButtonClass}
        onClick={() => {
          setArmed(false)
        }}
      >
        Cancel
      </button>
    </span>
  )
}
