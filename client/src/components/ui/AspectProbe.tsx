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
 * The ratio is reported rather than the size, so a caller can hold it in a
 * `useState` without a guard: a ref callback re-runs on every render, and a
 * fresh object each time would be a re-render that causes a re-render, where
 * the same number twice is a state update React bails out of.
 */
export interface AspectProbeProps {
  /** The image to measure. Fetched, never drawn anywhere a viewer can see. */
  url: string
  /** Called with width ÷ height once the browser knows it. */
  onAspect: (aspect: number) => void
}

export function AspectProbe({ url, onAspect }: AspectProbeProps) {
  return (
    <div aria-hidden className="pointer-events-none absolute h-0 w-0 overflow-hidden opacity-0">
      <Artwork
        url={url}
        shape="free"
        className="h-px w-px"
        onNaturalSize={(size) => {
          onAspect(size.width / size.height)
        }}
      />
    </div>
  )
}
