import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { buttonClass, type ButtonVariant } from '@/components/ui/styles'

export type { ButtonVariant }

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-pressed'> {
  /** `secondary` by default: the primary white pill is for one action a screen. */
  variant?: ButtonVariant
  /** Rendered before the label — a `PlayGlyph`, a count, a glyph. */
  iconLeft?: ReactNode
  /**
   * A toggle, and whether it is on. Sets `aria-pressed` (so a screen reader
   * announces the state the look is showing) and the pressed treatment from
   * `styles`. Omit it entirely for an ordinary button: `aria-pressed` on
   * something that does not stay pressed is a lie about the control.
   */
  pressed?: boolean
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
  pressed,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      aria-pressed={pressed}
      className={buttonClass(variant, className, { pressed })}
    >
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
