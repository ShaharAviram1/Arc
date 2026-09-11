import { cx } from '@/components/ui/styles'

/**
 * The shapes a page can be waiting for. A skeleton is the shape of the thing
 * that is coming, not a spinner: it tells you the page is a shelf of tiles
 * before the tiles arrive, and it does not move the layout when they do.
 */
export type SkeletonShape = 'hero' | 'tile' | 'key' | 'row' | 'text'

export interface SkeletonProps {
  /** `key` (a 2:3 card) by default. */
  shape?: SkeletonShape
  /** How many to draw. Ignored by `hero`, which is always one. */
  count?: number
  /**
   * What a screen reader is told while this is on screen. One live region per
   * skeleton block; pass `null` for a second block on a page that already has
   * one, so the page does not announce "loading" three times.
   */
  label?: string | null
  className?: string
}

const PULSE = 'animate-arcpulse bg-[rgba(255,255,255,0.07)]'

/** One placeholder block. */
function Block({ className }: { className?: string }) {
  return <div aria-hidden className={cx(PULSE, className)} />
}

/** A tile-shaped skeleton: the artwork, then the title and meta lines. */
function Tile({ aspect, radius }: { aspect: string; radius: string }) {
  return (
    <div className="w-full">
      <Block className={cx(aspect, radius, 'w-full')} />
      <Block className="mt-2.5 h-3.5 w-4/5 rounded-thumb" />
      <Block className="mt-1.5 h-3 w-1/2 rounded-thumb" />
    </div>
  )
}

/**
 * Loading, in the shape of what is loading.
 *
 * `role="status"` with a visually hidden sentence, because the blocks
 * themselves are `aria-hidden`: a screen reader has nothing to read in a
 * pulsing rectangle, and "loading" said once is the whole message.
 */
export function Skeleton({
  shape = 'key',
  count = 1,
  label = 'Loading…',
  className,
}: SkeletonProps) {
  const items = Array.from({ length: shape === 'hero' ? 1 : Math.max(1, count) }, (_, i) => i)

  const body =
    shape === 'hero' ? (
      <Block className="aspect-[21/9] w-full rounded-hero" />
    ) : shape === 'text' ? (
      <div className="flex flex-col gap-2">
        {items.map((i) => (
          <Block key={i} className="h-3.5 w-full max-w-[52ch] rounded-thumb" />
        ))}
      </div>
    ) : shape === 'row' ? (
      <div className="flex flex-col gap-0.5">
        {items.map((i) => (
          <Block key={i} className="h-[72px] w-full rounded-row" />
        ))}
      </div>
    ) : shape === 'tile' ? (
      <div className="flex gap-6 overflow-hidden">
        {items.map((i) => (
          <div key={i} className="w-[280px] shrink-0">
            <Tile aspect="aspect-[16/9]" radius="rounded-art" />
          </div>
        ))}
      </div>
    ) : (
      <div className="flex gap-6 overflow-hidden">
        {items.map((i) => (
          <div key={i} className="w-[172px] shrink-0">
            <Tile aspect="aspect-[2/3]" radius="rounded-art" />
          </div>
        ))}
      </div>
    )

  if (label === null) {
    return (
      <div aria-hidden className={className}>
        {body}
      </div>
    )
  }

  return (
    <div role="status" className={className}>
      <span className="sr-only">{label}</span>
      {body}
    </div>
  )
}
