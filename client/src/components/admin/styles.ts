/**
 * The admin page's class strings.
 *
 * Separate from `ui.tsx` because these are built by calling the shared
 * `buttonClass` / `inputClass` rather than written out as literals, and
 * `react-refresh/only-export-components` (an error here, via
 * `--max-warnings 0`) will not let a `.tsx` module export a computed constant
 * beside its components.
 *
 * The design is M15's: 44px controls everywhere, a white primary for the one
 * action a tab is for, glass for everything else, and the error hue only on
 * the controls that remove something.
 */

import { buttonClass, inputClass as fieldClass } from '@/components/ui'

/** The one white action a tab is allowed: Save rules, Create invite, Sweep. */
export const primaryButtonClass = buttonClass('primary', 'h-11 px-5 text-[15px]')

/** Glass, 44px: everything that acts but is not the action. */
export const subtleButtonClass = buttonClass('chip', 'h-11 px-4 text-[13px]')

/** Removes something. Says so in its own colour rather than in red-on-red. */
export const dangerButtonClass = buttonClass('danger', 'h-11 px-4 text-[13px]')

/** A form field. 44px, `--arc-surface-input`, the shared focus ring. */
export const inputClass = fieldClass()
