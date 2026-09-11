import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cx, rowClass } from '@/components/ui/styles'

interface RowCommon {
  children: ReactNode
  className?: string
}

export interface RowLinkProps extends RowCommon {
  /** Where the row goes. Makes the row a link rather than a button. */
  to: string
}

export interface RowButtonProps
  extends RowCommon, Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'className'> {
  to?: undefined
}

export type RowProps = RowLinkProps | RowButtonProps

/**
 * The grouped row: episodes on Show, shows on My List, log lines on MAL.
 *
 * The whole row is the target — 14px radius, a fill that arrives on hover —
 * because a row with a small action floating at its right edge is the thing
 * this design is replacing. A row that navigates renders as a `<Link>` so it
 * can be opened in a new tab; one that acts renders as a `<button>`.
 */
export function Row(props: RowProps) {
  if (props.to !== undefined) {
    const { to, children, className } = props
    return (
      <Link to={to} className={rowClass(className)}>
        {children}
      </Link>
    )
  }

  const { to, children, className, type = 'button', ...rest } = props
  void to
  return (
    <button {...rest} type={type} className={rowClass(className)}>
      {children}
    </button>
  )
}

/**
 * A stack of rows. 2px apart, so they read as one group rather than as a list
 * of separate cards, and no dividers: the hover fill does that work.
 */
export function RowGroup({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('flex flex-col gap-0.5', className)}>{children}</div>
}
