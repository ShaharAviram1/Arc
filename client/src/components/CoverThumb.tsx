interface CoverThumbProps {
  url: string | null
  /** Sizing (and any rounding) the caller wants; the rest is fixed. */
  className?: string
}

/**
 * A show's cover at whatever size the caller asks for, with the same neutral
 * block standing in when the catalogue has no image. Decorative in every use:
 * a cover always sits next to the title it belongs to, so it carries no alt
 * text and the placeholder is hidden from assistive tech.
 */
export function CoverThumb({ url, className = '' }: CoverThumbProps) {
  const shared = `shrink-0 bg-[var(--arc-surface-raised)] ${className}`

  if (url === null) {
    return (
      <div
        aria-hidden
        className={`flex items-center justify-center text-center text-[0.625rem] leading-tight text-[var(--arc-text-muted)] ${shared}`}
      >
        No cover
      </div>
    )
  }

  return <img src={url} alt="" loading="lazy" className={`object-cover ${shared}`} />
}
