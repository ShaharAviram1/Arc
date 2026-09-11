import type { ReactNode } from 'react'
import { cx } from '@/components/ui/styles'

export interface EmptyStateProps {
  /** What would fill this page, and how it gets filled. One or two sentences. */
  message: string
  /** An optional line above it, when "empty" needs naming. */
  title?: string
  /** One way out — a `Button` or a `Link` wearing `buttonClass`. */
  action?: ReactNode
  className?: string
}

/**
 * Nothing here, said calmly.
 *
 * A quiet card rather than a blank page, because an empty shelf and a broken
 * shelf look identical otherwise. One action at most: an empty state that
 * offers three things to try is a menu, and the person is already lost.
 */
export function EmptyState({ message, title, action, className }: EmptyStateProps) {
  return (
    <div
      className={cx(
        'rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] px-7 py-10 text-center',
        className,
      )}
    >
      {title === undefined ? null : (
        <p className="text-[16px] font-medium text-[var(--arc-text)]">{title}</p>
      )}
      <p
        className={cx(
          'mx-auto max-w-[46ch] text-[14px] leading-relaxed text-[var(--arc-text-muted)]',
          title === undefined ? '' : 'mt-2',
        )}
      >
        {message}
      </p>
      {action === undefined ? null : <div className="mt-5 flex justify-center">{action}</div>}
    </div>
  )
}
