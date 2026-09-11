import type { ReactNode } from 'react'
import { cx } from '@/components/ui/styles'

export interface EyebrowProps {
  children: ReactNode
  /**
   * `ember` is reserved for a broadcast that is happening tonight. Everything
   * else is `muted`.
   */
  tone?: 'muted' | 'ember'
  className?: string
}

/**
 * The small uppercase label above a title — "CONTINUE WATCHING", a weekday on
 * an appointment card.
 *
 * A `<p>`, not a heading: it labels the block it sits above but adds nothing
 * to the document outline, and a screen reader reading "continue watching,
 * heading level 3" before the actual title is noise.
 */
export function Eyebrow({ children, tone = 'muted', className }: EyebrowProps) {
  return (
    <p
      className={cx(
        'text-[12px] font-semibold tracking-[0.1em] uppercase',
        tone === 'ember' ? 'text-[var(--arc-ember)]' : 'text-[var(--arc-text-muted)]',
        className,
      )}
    >
      {children}
    </p>
  )
}
