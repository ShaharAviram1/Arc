import { useState, type ReactNode } from 'react'
import { Artwork } from '@/components/ui/Artwork'
import { AspectProbe } from '@/components/ui/AspectProbe'
import { PosterWash } from '@/components/ui/PosterWash'
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
 * - **Banner hero.** A banner no wider than {@link MAX_BANNER_ASPECT}, cropped
 *   to the frame, with the scrim and the title block over it. What the design
 *   drew. A 16:9 backdrop qualifies: filling a 21:9 frame with it costs a
 *   little off the top and bottom and nothing else.
 * - **Poster hero.** The frame is *filled* with the poster blurred past
 *   recognition and darkened — it is a colour wash, not a picture, so its
 *   resolution stops mattering — and the crisp poster is laid at its own 2:3
 *   ratio beside the title, around 180px wide on a laptop and as much as the
 *   frame's height allows below that. A poster is never scaled up to fill the
 *   frame, which is the whole point: upscaling a 230px MyAnimeList picture
 *   across 1180px is what made the hero look broken. This is what a show with
 *   no banner gets, and also what a show whose only wide art is an AniList
 *   strip gets: 1900 × 400 is 4.75:1, and `object-cover` in a 21:9 frame shows
 *   the middle half of one. With no poster to lay over it the strip is still
 *   the show's own artwork, so it becomes the wash. The treatment itself lives
 *   in `PosterWash`, because the 16:9 episode card has the same problem one
 *   size down (owner, 2026-09-12).
 *
 * Both images load eagerly: the hero is the largest thing above the fold on
 * both pages, and lazy-loading the one image a page is built around only
 * delays it.
 *
 * **The frame itself is always 21:9** (owner, 2026-09-12: "all heroes in the
 * homepage need to be in the same size"). It used to take the banner's own
 * ratio, clamped to between 21:9 and 3.6:1, which meant Watch Now's carousel
 * changed height between slides — a 16:9 backdrop landing on 21:9 and an
 * AniList strip on 3.6:1. One fixed frame, and the choice of treatment inside
 * it is what absorbs the difference. The banner's ratio is measured off-frame
 * (`AspectProbe`) so the frame never shows a strip on its way to the right
 * answer: until the ratio is known the wash is what renders.
 */

/**
 * Wider than this and a banner cannot honestly fill a 21:9 frame: 21:9 is
 * 2.33, so a 2.6:1 picture loses a sliver of its sides, and AniList's 4.75:1
 * strip would lose half of itself. Matches the episode card's rule one size
 * down, for the same reason and with a little more room, since the hero's
 * frame is the wider of the two.
 */
const MAX_BANNER_ASPECT = 2.6

/**
 * The poster is sized by the frame's height, not by a width of its own: a 2:3
 * poster is 1.5 times as tall as it is wide and a 21:9 frame is 2.3 times as
 * wide as it is tall, so any fixed width tall enough to read on a laptop is
 * taller than a phone's frame and would be clipped at the top. `h-full` with
 * `aspect-[2/3]` lets the ratio work out the width, and the cap keeps it near
 * the 180px the design asks for once the frame is big enough to allow it.
 */
const POSTER = 'max-h-[270px]'

/** The title block's gutter. Matches the banner hero's, so the two agree. */
const PADDING = 'p-6 sm:p-10'

export interface HeroFrameProps {
  /** AniList's banner, or null — the ordinary case for a MAL record. */
  banner: string | null
  /** The 2:3 key visual, largest first. Both the wash and the crisp poster. */
  poster: string | null
  /** The title block: eyebrow, title, native title, meta line. */
  children?: ReactNode
  className?: string
}

export function HeroFrame({ banner, poster, children, className }: HeroFrameProps) {
  // Keyed by the url it was measured from: Watch Now cycles a single frame
  // through several shows, and the previous slide's banner says nothing about
  // this one's. Null means "not known yet", which is the wash.
  const [measured, setMeasured] = useState<{ url: string; aspect: number } | null>(null)

  const art = banner === null || banner === '' ? null : banner
  const aspect = measured !== null && measured.url === art ? measured.aspect : null
  const frame = cx('w-full shadow-hero', className)

  if (art !== null && aspect !== null && aspect <= MAX_BANNER_ASPECT) {
    return (
      <Artwork url={art} shape="hero" scrim eager className={frame}>
        <div className={cx('absolute inset-0 flex items-end', PADDING)}>
          <div className="max-w-[620px]">{children}</div>
        </div>
      </Artwork>
    )
  }

  return (
    <>
      <PosterWash
        poster={poster}
        // With no poster the banner is the only artwork there is: too wide to
        // fill the frame as a picture, fine as a ground once it is blurred.
        ground={poster ?? art}
        shape="hero"
        eager
        padding={PADDING}
        plateClassName={POSTER}
        className={frame}
      >
        {children}
      </PosterWash>
      {art !== null && aspect === null ? (
        <AspectProbe
          url={art}
          onAspect={(next) => {
            setMeasured({ url: art, aspect: next })
          }}
        />
      ) : null}
    </>
  )
}
