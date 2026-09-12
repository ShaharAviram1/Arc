import type { ReactNode } from 'react'
import { cx } from '@/components/ui/styles'

/**
 * The shapes artwork is allowed to take. Anime's native artwork is the tall
 * key visual, so unlike a 16:9 streaming app this design is a two-ratio world
 * and each ratio carries a fixed meaning:
 *
 * - `key`   2:3 — the show, in shelves and grids
 * - `thumb` 2:3 — the show, small, in rows and appointment cards
 * - `still` 16:9 — a single episode
 * - `hero`  21:9 — the framed card at the top of Home and Show
 * - `free`  — no ratio and no radius; the caller's classes decide. Only for
 *   callers that predate this pass (see `CoverThumb`).
 */
export type ArtworkShape = 'key' | 'thumb' | 'still' | 'hero' | 'free'

/** Radii, by the thing they belong to. `none` leaves it to the caller. */
export type ArtworkRadius = 'hero' | 'card' | 'row' | 'art' | 'still' | 'thumb' | 'none'

const SHAPE_ASPECT: Record<ArtworkShape, string> = {
  key: 'aspect-[2/3]',
  thumb: 'aspect-[2/3]',
  still: 'aspect-[16/9]',
  hero: 'aspect-[21/9]',
  free: '',
}

const SHAPE_RADIUS: Record<ArtworkShape, ArtworkRadius> = {
  key: 'art',
  thumb: 'thumb',
  still: 'art',
  hero: 'hero',
  free: 'none',
}

const RADIUS_CLASS: Record<ArtworkRadius, string> = {
  hero: 'rounded-hero',
  card: 'rounded-card',
  row: 'rounded-row',
  art: 'rounded-art',
  still: 'rounded-still',
  thumb: 'rounded-thumb',
  none: '',
}

export interface ArtworkProps {
  /** The catalogue's image, or `null` when it has none. */
  url: string | null
  /** Ratio and default radius. `key` (2:3) by default. */
  shape?: ArtworkShape
  /** Overrides the radius the shape would pick — an episode still in a row is `still`. */
  radius?: ArtworkRadius
  /**
   * Playback progress, 0–1. Draws the 3px white strip flush to the bottom
   * edge. White, always: the arc gradient means season progress in My List
   * and nothing else. Omit (or pass 0) for an unstarted episode.
   */
  progress?: number | null
  /** Lays the hero's scrim over the art so a title stays legible on any still. */
  scrim?: boolean
  /**
   * Loads the image at once instead of when it scrolls into view. For the one
   * or two images a page is built around — a hero's backdrop and its poster —
   * which are above the fold by definition and which `loading="lazy"` only
   * delays. Everything else stays lazy.
   */
  eager?: boolean
  /**
   * Called with the image's intrinsic size once the browser knows it. The one
   * thing a frame cannot work out from the URL, and what the two wide frames
   * pick their treatment from — see `AspectProbe`.
   */
  onNaturalSize?: (size: { width: number; height: number }) => void
  /**
   * Artwork sits beside the title it belongs to almost everywhere, so it is
   * decorative by default and carries no alt text. Pass one only where the
   * image is the only thing naming the show.
   */
  alt?: string
  /** Sizing, shadow and layout — everything this component does not decide. */
  className?: string
  /** Overlaid on the art: a hero's title block, a watch-order chip. */
  children?: ReactNode
}

/** Clamps to the 0–1 the strip can actually draw. */
function fraction(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

/**
 * A piece of catalogue artwork, framed.
 *
 * Framed, never bled: a hairline at 0.5px, a radius that says what the thing
 * is, and the title below the image rather than burned over it. When the
 * catalogue has no image the box keeps its shape and fills with the
 * diagonal-stripe placeholder — a missing cover must not collapse a row.
 */
export function Artwork({
  url,
  shape = 'key',
  radius,
  progress = null,
  scrim = false,
  eager = false,
  onNaturalSize,
  alt = '',
  className,
  children,
}: ArtworkProps) {
  const rounded = RADIUS_CLASS[radius ?? SHAPE_RADIUS[shape]]
  const decorative = alt === ''
  const shown = progress === null ? 0 : fraction(progress)

  /**
   * Reported from both the ref and `onLoad`: a cached image can be complete
   * before React ever attaches a handler, and a cold one has no size until it
   * loads. Whichever happens first wins; a size of 0 is "not yet".
   */
  function measure(image: HTMLImageElement | null): void {
    if (image === null || onNaturalSize === undefined) return
    const { naturalWidth, naturalHeight } = image
    if (naturalWidth > 0 && naturalHeight > 0) {
      onNaturalSize({ width: naturalWidth, height: naturalHeight })
    }
  }

  return (
    <div
      className={cx(
        'relative isolate overflow-hidden border-[0.5px] border-[var(--arc-border)] bg-[rgba(255,255,255,0.04)]',
        SHAPE_ASPECT[shape],
        rounded,
        className,
      )}
    >
      {url === null ? (
        <div
          className="art-placeholder h-full w-full"
          {...(decorative ? { 'aria-hidden': true } : { role: 'img', 'aria-label': alt })}
        />
      ) : (
        <img
          ref={measure}
          onLoad={(event) => {
            measure(event.currentTarget)
          }}
          src={url}
          alt={alt}
          loading={eager ? 'eager' : 'lazy'}
          decoding="async"
          className="h-full w-full object-cover"
        />
      )}

      {scrim ? <div aria-hidden className="art-scrim absolute inset-0" /> : null}

      {children}

      {shown > 0 ? (
        <div
          aria-hidden
          className="absolute inset-x-0 bottom-0 h-[3px] bg-[var(--arc-progress-track)]"
        >
          <div
            className="h-full bg-[var(--arc-progress-fill)]"
            style={{ width: `${String(shown * 100)}%` }}
          />
        </div>
      ) : null}
    </div>
  )
}
