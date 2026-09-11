import { cx } from '@/components/ui/styles'

export interface SegmentedOption<T extends string> {
  value: T
  label: string
  /** A count or a hint shown after the label, e.g. a pending total. */
  hint?: string
}

export interface SegmentedProps<T extends string> {
  /** Names the group for assistive tech — "Cour", "Review queue". */
  label: string
  options: readonly SegmentedOption<T>[]
  value: T
  onChange: (value: T) => void
  className?: string
}

/**
 * A segmented control: the cour selector on Show, the tabs on Review and
 * Admin.
 *
 * Buttons with `aria-pressed` in a labelled group rather than a radio group,
 * because these are filters over content already on the page, not a value
 * being entered into a form. Every button is 44px tall — the design has no
 * exceptions to that anywhere.
 */
export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
  className,
}: SegmentedProps<T>) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cx(
        'inline-flex max-w-full gap-0.5 overflow-x-auto rounded-segmented bg-[var(--arc-nav-active)] p-0.5',
        'no-scrollbar',
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            onClick={() => {
              onChange(option.value)
            }}
            className={cx(
              'inline-flex h-11 shrink-0 items-center gap-2 whitespace-nowrap rounded-segment px-[18px] text-[15px] transition-colors duration-200',
              active
                ? 'bg-[rgba(255,255,255,0.18)] font-semibold text-[var(--arc-text)]'
                : 'text-[var(--arc-text-muted)] hover:text-[var(--arc-text)]',
              'focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--arc-focus)]',
            )}
          >
            {option.label}
            {option.hint === undefined ? null : (
              <span className="text-[13px] tabular-nums text-[var(--arc-text-muted)]">
                {option.hint}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
