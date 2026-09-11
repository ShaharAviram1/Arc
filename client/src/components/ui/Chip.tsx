import type { ButtonHTMLAttributes } from 'react'
import { chipClass } from '@/components/ui/styles'

export interface ChipProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-pressed'> {
  /** Whether this is the filter currently applied. */
  active?: boolean
}

/**
 * A filter chip — genres on Browse, list statuses on My List.
 *
 * `aria-pressed` rather than a radio group: the chips are toggles over a list
 * that is already on screen, and a screen reader saying "pressed" is the
 * truthful description of a filter that is on. The active one is not a
 * different colour, only a brighter surface and a heavier label; colour in
 * this design means state (ember: tonight), never selection.
 */
export function Chip({ active = false, className, type = 'button', children, ...rest }: ChipProps) {
  return (
    <button {...rest} type={type} aria-pressed={active} className={chipClass(active, className)}>
      {children}
    </button>
  )
}
