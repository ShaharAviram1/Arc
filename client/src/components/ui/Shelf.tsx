import type { ReactNode } from 'react'
import { cx } from '@/components/ui/styles'

export interface ShelfProps {
  /** The section heading, e.g. "Up Next". */
  title: string
  /** One line under it saying what is in the shelf and why. */
  lede?: string
  /** Right of the heading — a "See all" link, a segmented control. */
  action?: ReactNode
  /** The tiles. Each becomes a snap point and is kept from shrinking. */
  children: ReactNode
  /** Spacing around the section, which belongs to the page. */
  className?: string
  /** Extra classes for the scroller itself (a grid instead of a rail, say). */
  scrollerClassName?: string
}

/**
 * A horizontal shelf: heading, one explanatory line, and a snapping scroller.
 *
 * The scrollbar is hidden and the items snap, so the rail stops between tiles
 * rather than halfway through one. Items are not wrapped in extra elements —
 * the shelf reaches into its children for `snap-start` and `shrink-0` instead,
 * so a caller can put a `<Link>`, an `<article>` or a `<li>` in it without
 * this component having an opinion.
 */
export function Shelf({ title, lede, action, children, className, scrollerClassName }: ShelfProps) {
  return (
    <section className={cx('min-w-0', className)}>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[24px] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
            {title}
          </h2>
          {lede === undefined ? null : (
            <p className="mt-1.5 max-w-[66ch] text-[14px] text-[var(--arc-text-muted)]">{lede}</p>
          )}
        </div>
        {action}
      </div>

      <div
        className={cx(
          'no-scrollbar mt-[18px] flex snap-x snap-mandatory gap-6 overflow-x-auto pb-1',
          '[&>*]:shrink-0 [&>*]:snap-start',
          scrollerClassName,
        )}
      >
        {children}
      </div>
    </section>
  )
}
