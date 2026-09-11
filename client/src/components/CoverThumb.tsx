import { Artwork } from '@/components/ui/Artwork'

interface CoverThumbProps {
  url: string | null
  /** Sizing (and any rounding) the caller wants; the rest is fixed. */
  className?: string
}

/**
 * A show's cover at whatever size the caller asks for.
 *
 * Now a thin wrapper over `Artwork` with no shape of its own, so the pages
 * written before the M15 design pass keep the sizing and rounding they pass
 * in while picking up the new framing — the 0.5px hairline and the
 * diagonal-stripe placeholder. New code should reach for `Artwork` and one of
 * its named shapes (`key`, `thumb`, `still`, `hero`) instead; this exists so
 * the restyle of the pages can happen one page at a time.
 *
 * Decorative in every use: a cover always sits next to the title it belongs
 * to, so it carries no alt text and the placeholder is hidden from assistive
 * tech.
 */
export function CoverThumb({ url, className = '' }: CoverThumbProps) {
  return <Artwork url={url} shape="free" className={`shrink-0 ${className}`} />
}
