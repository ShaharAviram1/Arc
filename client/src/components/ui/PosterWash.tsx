import type { ReactNode } from 'react'
import { Artwork, type ArtworkShape } from '@/components/ui/Artwork'
import { cx } from '@/components/ui/styles'

/**
 * A wide frame filled with artwork that is not wide: the poster blurred past
 * recognition and darkened to a colour wash, with the crisp poster laid over
 * it at its own 2:3 ratio.
 *
 * Two frames need it, for the same reason. The hero (`HeroFrame`) is 21:9 and
 * a record filled from MyAnimeList carries a 230–425 px picture and no banner
 * at all, so there is nothing to fill it with honestly. The 16:9 episode card
 * on Watch Now is the same problem one size down: an episode with no still of
 * its own, on a show whose only wide art is an AniList banner — 1900 × 400,
 * which `object-cover` in a 280 px card crops to a 3× zoom of a sliver of it
 * (owner, 2026-09-12, "continue watching posters need adjustment").
 *
 * The ground is the poster by default; a caller with something else to blur —
 * a hero holding a banner too wide to fill its frame — passes `ground`.
 *
 * A poster is never scaled up to fill the frame, which is the whole point: the
 * wash is a ground rather than a picture, so its resolution stops mattering,
 * and the only thing shown at full size is shown at its own ratio.
 *
 * The two data attributes are named for the hero because that is where the
 * treatment came from, and the hero's tests already find its parts by them.
 */

/**
 * `scale(1.15)` so the blur's transparent fringe is pushed outside the frame
 * and clipped rather than showing as a soft border; brightness and saturation
 * turn a photograph into a ground the white title survives on.
 */
export const BACKDROP =
  'absolute inset-0 h-full w-full scale-[1.15] object-cover blur-[40px] brightness-[0.5] saturate-[1.2]'

export interface PosterWashProps {
  /** The 2:3 key visual — both the wash and the crisp plate over it. */
  poster: string | null
  /**
   * What to blur into the ground, when it is not the poster. For a hero whose
   * only artwork is a banner too wide to fill the frame: unusable as a
   * picture, perfectly good as a colour once it is blurred. Defaults to the
   * poster, which is the ordinary case.
   */
  ground?: string | null
  /** The frame's ratio: `hero` for a hero, `still` for an episode card. */
  shape: ArtworkShape
  /** Loads both copies at once. For a hero, which is above the fold. */
  eager?: boolean
  /** Playback progress, passed through to the frame's strip. */
  progress?: number | null
  /** The gutter the plate and anything beside it sit in. */
  padding?: string
  /** Extra classes on the crisp plate — the hero caps its height. */
  plateClassName?: string
  /** Centres the plate when there is nothing beside it (an episode card). */
  align?: 'start' | 'center'
  /** Sizing and shadow for the frame itself. */
  className?: string
  /** Laid beside the plate: a hero's title block. Nothing on a card. */
  children?: ReactNode
}

export function PosterWash({
  poster,
  ground,
  shape,
  eager = false,
  progress = null,
  padding,
  plateClassName,
  align = 'start',
  className,
  children,
}: PosterWashProps) {
  const wash = ground === undefined ? poster : ground

  return (
    <Artwork url={null} shape={shape} progress={progress} className={className}>
      {wash === null ? null : (
        <img
          data-hero-backdrop
          src={wash}
          alt=""
          aria-hidden
          loading={eager ? 'eager' : 'lazy'}
          decoding="async"
          className={BACKDROP}
        />
      )}
      {/* After the backdrop, before the content: the wash is the ground, the
          scrim is what keeps white type legible on whatever colour it is. */}
      <div aria-hidden className="art-scrim absolute inset-0" />

      <div
        className={cx(
          'absolute inset-0 flex items-end gap-5 sm:gap-7',
          align === 'center' ? 'justify-center' : null,
          padding,
        )}
      >
        {/* No poster means no artwork at all, and a striped 2:3 box laid on a
            striped frame says nothing twice: whatever is beside it carries it
            alone. */}
        {poster === null ? null : (
          <div data-hero-poster className="flex h-full shrink-0 items-end">
            <Artwork
              url={poster}
              shape="key"
              eager={eager}
              className={cx('h-full w-auto shadow-tile', plateClassName)}
            />
          </div>
        )}
        {children === undefined ? null : <div className="min-w-0 max-w-[620px]">{children}</div>}
      </div>
    </Artwork>
  )
}
