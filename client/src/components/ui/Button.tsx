import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { buttonClass, type ButtonVariant } from '@/components/ui/styles'

export type { ButtonVariant }

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** `secondary` by default: the primary white pill is for one action a screen. */
  variant?: ButtonVariant
  /** Rendered before the label — a `PlayGlyph`, a count, a glyph. */
  iconLeft?: ReactNode
}

/**
 * Every button in Arc, in four dresses.
 *
 * `type` defaults to `button` because the common mistake it prevents — a
 * control inside a form quietly submitting it — is a behaviour bug, not a
 * styling one. Anything that needs to be a link (`<Link>`, `<a>`) uses
 * `buttonClass` instead; there is no `as` prop, since a link that is not a
 * link is worse than two call sites.
 */
export function Button({
  variant = 'secondary',
  iconLeft,
  className,
  type = 'button',
  children,
  ...rest
}: ButtonProps) {
  return (
    <button {...rest} type={type} className={buttonClass(variant, className)}>
      {iconLeft}
      {children}
    </button>
  )
}

/**
 * The play triangle, drawn in borders rather than shipped as an icon: the
 * design has no icon set, and a CSS triangle inherits `currentColor`, so it
 * is black on the white pill and white on a glass one without a second asset.
 */
export function PlayGlyph() {
  return (
    <span
      aria-hidden
      className="inline-block h-0 w-0 border-y-[6px] border-l-[9px] border-y-transparent border-l-current"
    />
  )
}
