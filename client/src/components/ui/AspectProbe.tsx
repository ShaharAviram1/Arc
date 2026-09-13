import { rememberAspect } from '@/components/ui/aspect'
import { Artwork } from '@/components/ui/Artwork'

/**
 * Loads an image off-frame so its shape can be measured before anything shows
 * it.
 *
 * Two frames pick their treatment from the ratio of a picture that has not
 * loaded yet — the hero (`HeroFrame`) and the episode card on Watch Now — and
 * in both of them rendering the picture to find out is the zoomed strip the
 * rule exists to prevent. So: an `Artwork` like any other, a copy the layout
 * cannot see, reporting the one thing only the browser knows.
 *
 * Every measurement is remembered for the tab (`rememberAspect`), so a probe
 * is a question asked once: a frame that mounts later with the same url reads
 * the answer synchronously and never shows the wash (owner, 2026-09-13). A
 * caller that only wants the url measured — Watch Now's hero, which probes
 * all six slides up front — can leave `onAspect` off entirely and let the
 * store carry it.
 *
 * The ratio is reported rather than the size, so a caller can hold it in a
 * `useState` without a guard: a ref callback re-runs on every render, and a
 * fresh object each time would be a re-render that causes a re-render, where
 * the same number twice is a state update React bails out of.
 *
 * `eager`, and this is the whole reason the probe works: the copy sits in a
 * 0×0 box, and Chrome never fetches a `loading="lazy"` image that has no box
 * to scroll into view. Lazy, the ratio was never reported, so every hero
 * whose banner had not already been cached by some other card stayed on the
 * blurred-poster fallback for good (owner, 2026-09-13).
 */
export interface AspectProbeProps {
  /** The image to measure. Fetched, never drawn anywhere a viewer can see. */
  url: string
  /**
   * Called with width ÷ height once the browser knows it. Optional: the
   * measurement is remembered either way, and a frame reading the store is
   * the ordinary consumer.
   */
  onAspect?: (aspect: number) => void
}

export function AspectProbe({ url, onAspect }: AspectProbeProps) {
  return (
    <div
      aria-hidden
      data-aspect-probe
      className="pointer-events-none absolute h-0 w-0 overflow-hidden opacity-0"
    >
      <Artwork
        url={url}
        shape="free"
        eager
        className="h-px w-px"
        onNaturalSize={(size) => {
          const aspect = size.width / size.height
          rememberAspect(url, aspect)
          onAspect?.(aspect)
        }}
      />
    </div>
  )
}
