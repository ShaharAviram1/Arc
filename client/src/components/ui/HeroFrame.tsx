import { useState, type ReactNode } from 'react'
import { Artwork } from '@/components/ui/Artwork'
import { cx } from '@/components/ui/styles'

/**
 * The 21:9 frame at the top of Watch Now and of a show page (M15).
 *
 * It exists because the two heroes have to answer the same awkward question:
 * anime's native artwork is a 2:3 poster, and only AniList's banner is
 * actually 21:9. A record filled from MyAnimeList carries a 230–425 px
 * picture and no banner at all — and stretching that across a 1180 px frame
 * is the "extremely low quality" hero the owner reported on 2026-09-11.
 *
 * So there are two heroes, not one:
 *
 * - **Banner hero.** AniList's ~1900 px banner, cropped to the frame, with
 *   the scrim and the title block over it. What the design drew.
 * - **Poster hero.** No banner: the frame is *filled* with the poster blurred
 *   past recognition and darkened — it is a colour wash, not a picture, so its
 *   resolution stops mattering — and the crisp poster is laid at its own 2:3
 *   ratio beside the title, around 180px wide on a laptop and as much as the
 *   frame's height allows below that. A poster is never scaled up to fill the
 *   frame, which is the whole point: upscaling a 230px MyAnimeList picture
 *   across 1180px is what made the hero look broken.
 *
 * Both images load eagerly: the hero is the largest thing above the fold on
 * both pages, and lazy-loading the one image a page is built around only
 * delays it.
 *
 * A banner hero also sizes its *frame* to the banner (owner, 2026-09-12: "too
 * zoomed in"). AniList's banners are about 1900 × 400 — roughly 4.75:1 — and
 * `object-fit: cover` in a 2.33:1 frame shows only the middle ~half of one.
 * So the frame takes the image's own ratio once the browser knows it, clamped
 * to between 21:9 and 3.6:1: never narrower than the hero the design drew,
 * never so wide that it becomes a letterbox strip, and wide enough that an
 * AniList banner loses a sliver rather than half of itself. A 16:9 backdrop
 * from some later source still lands on 21:9, which is the ratio the design
 * is built around.
 */

/**
 * The poster is sized by the frame's height, not by a width of its own: a 2:3
 * poster is 1.5 times as tall as it is wide and a 21:9 frame is 2.3 times as
 * wide as it is tall, so any fixed width tall enough to read on a laptop is
 * taller than a phone's frame and would be clipped at the top. `h-full` with
 * `aspect-[2/3]` lets the ratio work out the width, and the cap keeps it near
 * the 180px the design asks for once the frame is big enough to allow it.
 */
const POSTER = 'h-full max-h-[270px] w-auto shadow-tile'

/** The title block's gutter. Matches the banner hero's, so the two agree. */
const PADDING = 'p-6 sm:p-10'

/** Never narrower than the hero the design drew: 21:9 is the floor and the default. */
const MIN_ASPECT = 21 / 9

/** And never a letterbox strip: past this a banner is cropped rather than obeyed. */
const MAX_ASPECT = 3.6

/**
 * The banner's own ratio, in the range a hero is allowed to take. Rounded to
 * three places because it is going into a style attribute and nothing on a
 * screen can tell 2.3333333333333335 from 2.333.
 */
function heroAspect(width: number, height: number): number {
  if (width <= 0 || height <= 0) return MIN_ASPECT
  const clamped = Math.min(MAX_ASPECT, Math.max(MIN_ASPECT, width / height))
  return Math.round(clamped * 1000) / 1000
}

/**
 * `scale(1.15)` so the blur's transparent fringe is pushed outside the frame
 * and clipped rather than showing as a soft border; brightness and saturation
 * turn a photograph into a ground the white title survives on.
 */
const BACKDROP =
  'absolute inset-0 h-full w-full scale-[1.15] object-cover blur-[40px] brightness-[0.5] saturate-[1.2]'

export interface HeroFrameProps {
  /** AniList's 21:9 banner, or null — the ordinary case for a MAL record. */
  banner: string | null
  /** The 2:3 key visual, largest first. Both the wash and the crisp poster. */
  poster: string | null
  /** The title block: eyebrow, title, native title, meta line. */
  children?: ReactNode
  className?: string
}

export function HeroFrame({ banner, poster, children, className }: HeroFrameProps) {
  // Null until the banner has loaded, which is what leaves the shape's own
  // 21:9 in charge until then; the transition carries the one step from there
  // to the banner's ratio. Deliberately *not* reset when the banner changes:
  // Watch Now cycles through banners that are all much the same shape, and
  // collapsing to 21:9 and back on every slide is a worse frame than a ratio
  // that only moves when the next banner really is a different shape.
  const [size, setSize] = useState<{ width: number; height: number } | null>(null)

  const frame = cx('w-full shadow-hero', className)

  if (banner !== null && banner !== '') {
    return (
      <Artwork
        url={banner}
        shape="hero"
        scrim
        eager
        aspect={size === null ? null : heroAspect(size.width, size.height)}
        // Kept identical when the size has not actually changed, so React
        // bails out: a ref callback is re-run on every render, and a fresh
        // object each time would be a re-render that causes a re-render.
        onNaturalSize={(next) => {
          setSize((current) =>
            current !== null && current.width === next.width && current.height === next.height
              ? current
              : next,
          )
        }}
        className={cx(frame, 'transition-[aspect-ratio] duration-200 ease-arc')}
      >
        <div className={cx('absolute inset-0 flex items-end', PADDING)}>
          <div className="max-w-[620px]">{children}</div>
        </div>
      </Artwork>
    )
  }

  return (
    <Artwork url={null} shape="hero" className={frame}>
      {poster === null ? null : (
        <img
          data-hero-backdrop
          src={poster}
          alt=""
          aria-hidden
          loading="eager"
          decoding="async"
          className={BACKDROP}
        />
      )}
      {/* After the backdrop, before the content: the wash is the ground, the
          scrim is what keeps white type legible on whatever colour it is. */}
      <div aria-hidden className="art-scrim absolute inset-0" />

      <div className={cx('absolute inset-0 flex items-end gap-5 sm:gap-7', PADDING)}>
        {/* No poster means no artwork at all, and a striped 2:3 box laid on a
            striped frame says nothing twice: the title carries it alone. */}
        {poster === null ? null : (
          <div data-hero-poster className="flex h-full shrink-0 items-end">
            <Artwork url={poster} shape="key" eager className={POSTER} />
          </div>
        )}
        <div className="min-w-0 max-w-[620px]">{children}</div>
      </div>
    </Artwork>
  )
}
