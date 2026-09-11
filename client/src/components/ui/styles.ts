/**
 * Class-name builders shared by the UI primitives.
 *
 * They live in a `.ts` file rather than beside their components because
 * `react-refresh/only-export-components` (an error here, via
 * `--max-warnings 0`) forbids exporting a function that is not a component
 * from a `.tsx` module. The practical upside is that anything that cannot be
 * a `<button>` — a react-router `<Link>` styled as one, an `<a>` that leaves
 * the app — can still wear the same clothes.
 */

/** Joins class names, dropping the empty and conditional ones. */
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter((part): part is string => typeof part === 'string' && part !== '').join(' ')
}

/**
 * The one focus ring in Arc. Cold-light blue rather than ember: a focus ring
 * has to be legible over artwork, over a white button and over the ground,
 * and it must not be mistaken for "tonight's broadcast".
 */
export const FOCUS_RING =
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-focus)]'

export type ButtonVariant = 'primary' | 'secondary' | 'chip' | 'danger'

const BUTTON_BASE =
  'inline-flex shrink-0 items-center justify-center gap-[10px] whitespace-nowrap transition-[transform,background-color,border-color,color] duration-[220ms] ease-arc disabled:cursor-not-allowed disabled:opacity-60'

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  // The primary action is white. There is one per screen, and it is the thing
  // the screen is for: Play, Resume, Get picks, Confirm.
  primary:
    'h-12 rounded-full px-[26px] text-[16px] font-semibold bg-[var(--arc-action)] text-[var(--arc-action-ink)] hover:scale-[1.02]',
  // Glass: a real control, but not the one being offered.
  secondary:
    'h-12 rounded-full px-6 text-[16px] border-[0.5px] border-[var(--arc-border-strong)] bg-[var(--arc-surface-raised)] backdrop-blur-glass text-[var(--arc-text)] hover:bg-[rgba(255,255,255,0.13)]',
  // A 44px control for toolbars and inline actions.
  chip: 'h-11 rounded-full px-[18px] text-[15px] border-[0.5px] border-[var(--arc-border)] bg-[rgba(255,255,255,0.05)] text-[var(--arc-text-muted)] hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--arc-text)]',
  // Destructive, and it says so in its own colour rather than in red-on-red.
  danger:
    'h-11 rounded-full px-[18px] text-[15px] border-[0.5px] border-[color-mix(in_srgb,var(--arc-error)_40%,transparent)] bg-[color-mix(in_srgb,var(--arc-error)_12%,transparent)] text-[var(--arc-error)] hover:bg-[color-mix(in_srgb,var(--arc-error)_18%,transparent)]',
}

/** The classes for a button variant, for anything that cannot be a `Button`. */
export function buttonClass(variant: ButtonVariant = 'secondary', className?: string): string {
  return cx(BUTTON_BASE, BUTTON_VARIANT[variant], FOCUS_RING, className)
}

/** A filter chip: 44px, pill, quiet until it is the one that is on. */
export function chipClass(active: boolean, className?: string): string {
  return cx(
    'inline-flex h-11 shrink-0 items-center justify-center whitespace-nowrap rounded-full border-[0.5px] px-[18px] text-[15px] transition-colors duration-200',
    active
      ? 'border-[rgba(255,255,255,0.24)] bg-[rgba(255,255,255,0.14)] font-semibold text-[var(--arc-text)]'
      : 'border-[var(--arc-border)] bg-[rgba(255,255,255,0.05)] text-[var(--arc-text-muted)] hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--arc-text)]',
    FOCUS_RING,
    className,
  )
}

/**
 * A form field — text input, number, search, select, textarea.
 *
 * 44px tall like every other control in this design, on the translucent
 * `--arc-surface-input` rather than the window colour, because a field that is
 * *darker* than the page reads as a hole punched in it. The focus ring is the
 * shared one; `outline-offset-0` so it hugs the field rather than floating a
 * ring around it.
 *
 * A `<textarea>` must pass `h-auto` and its own vertical padding: the fixed
 * height is right for one line of input and wrong for several, and no text
 * container in this design may have a fixed height (Dynamic Type).
 */
export function inputClass(className?: string): string {
  return cx(
    'h-11 rounded-[12px] border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface-input)] px-3.5 text-[15px] text-[var(--arc-text)]',
    'placeholder:text-[var(--arc-text-muted)] disabled:cursor-not-allowed disabled:opacity-60 read-only:text-[var(--arc-text-muted)]',
    'focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-focus)]',
    className,
  )
}

/** A field label: 14px, the same on every form in Arc. */
export const LABEL_CLASS = 'block text-[14px] font-medium text-[var(--arc-text)]'

/** A field-level complaint: 13px, in the error hue, never larger. */
export const FIELD_ERROR_CLASS = 'text-[13px] text-[var(--arc-error)]'

/**
 * The grouped row: the full width is the target, and the only thing that
 * happens on hover is that the fill arrives.
 */
export function rowClass(className?: string): string {
  return cx(
    'flex w-full items-center gap-5 rounded-row px-[14px] py-3 text-left transition-colors duration-200 hover:bg-[var(--arc-surface-hover)]',
    FOCUS_RING,
    className,
  )
}
