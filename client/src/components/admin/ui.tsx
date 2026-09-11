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
 * The design language is M15's (`design/arc-design/.../README.md`): the
 * translucent `--arc-surface` cards, 0.5px hairlines, 44px controls, glass
 * secondaries and a white primary. Tables stay tables — five of them, every
 * column load-bearing — but at 13–14px with hairline row rules instead of
 * boxes, and each scrolls inside itself rather than pushing the page sideways.
 */

import { useState, type ReactNode } from 'react'
import { dangerButtonClass, subtleButtonClass } from '@/components/admin/styles'

export const panelClass =
  'rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-[18px]'

/** The header row reads as an eyebrow, not as a second set of labels. */
export const thClass =
  'px-3.5 py-3 text-left text-[12px] font-semibold tracking-[0.08em] text-[var(--arc-text-muted)] uppercase'

export const tdClass = 'px-3.5 py-3 align-top text-[14px] text-[var(--arc-text)]'

/** A section heading, so every tab's headings are the same size and weight. */
export function SectionHeading({ children }: { children: ReactNode }) {
  return (
    <h2 className="text-[24px] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
      {children}
    </h2>
  )
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
    <div className="overflow-x-auto rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)]">
      {children}
    </div>
  )
}

export type Tone = 'ok' | 'warn' | 'bad' | 'muted' | 'busy'

// Status only ever speaks in ok / warn / error, plus the cold focus blue for
// "in flight". Ember is reserved for a broadcast happening tonight and never
// appears on this page.
const TONE_CLASSES: Record<Tone, string> = {
  ok: 'text-[var(--arc-ok)] border-[var(--arc-ok)]/40 bg-[var(--arc-ok)]/10',
  warn: 'text-[var(--arc-warn)] border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10',
  bad: 'text-[var(--arc-error)] border-[var(--arc-error)]/40 bg-[var(--arc-error)]/10',
  busy: 'text-[var(--arc-focus)] border-[var(--arc-focus)]/40 bg-[var(--arc-focus)]/10',
  muted: 'text-[var(--arc-text-muted)] border-[var(--arc-border)] bg-[var(--arc-surface-raised)]',
}

/** A small coloured label: a status, a count, a state. */
export function Pill({ tone = 'muted', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border-[0.5px] px-2.5 py-0.5 text-[12px] whitespace-nowrap ${TONE_CLASSES[tone]}`}
    >
      {children}
    </span>
  )
}

/** One failed action, said next to the control that caused it. */
export function InlineError({ message, className = '' }: { message: string; className?: string }) {
  return (
    <p role="alert" className={`text-[13px] text-[var(--arc-error)] ${className}`}>
      {message}
    </p>
  )
}

/** One thing that worked, said the same way in every tab. */
export function Notice({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <p role="status" className={`text-[13px] text-[var(--arc-ok)] ${className}`}>
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
      <span className="text-[13px] text-[var(--arc-text-muted)]">{question}</span>
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
