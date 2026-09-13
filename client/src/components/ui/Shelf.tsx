import { useCallback, useEffect, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { cx, FOCUS_RING, GLASS_CIRCLE } from '@/components/ui/styles'

export interface ShelfProps {
  /** The section heading, e.g. "Up Next". */
  title: string
  /** One line under it saying what is in the shelf and why. */
  lede?: string
  /** Right of the heading — a "See all" link, a segmented control. */
  action?: ReactNode
  /** The tiles. Each becomes a snap point and is kept from shrinking. */
  children: ReactNode
  /** Spacing around the section, which belongs to the page. */
  className?: string
  /** Extra classes for the scroller itself (a grid instead of a rail, say). */
  scrollerClassName?: string
}

/**
 * How far a mouse has to travel before the gesture is a drag and not a click.
 * Six pixels is below what a hand does while pressing a button and well under
 * what a hand does when it means to move the rail.
 */
const DRAG_THRESHOLD_PX = 6

/**
 * An arrow moves almost a full strip: enough that the next press shows new
 * tiles, short of a full one so the tile that was at the edge stays as the
 * viewer's place in the rail.
 */
const ARROW_STEP = 0.9

/** Sub-pixel scroll positions are the rule, so the edges have a pixel of slack. */
const EDGE_SLACK_PX = 1

interface Edges {
  /** There is more rail than there is room for it. */
  overflowing: boolean
  atStart: boolean
  atEnd: boolean
}

const NO_OVERFLOW: Edges = { overflowing: false, atStart: true, atEnd: true }

function sameEdges(left: Edges, right: Edges): boolean {
  return (
    left.overflowing === right.overflowing &&
    left.atStart === right.atStart &&
    left.atEnd === right.atEnd
  )
}

/**
 * The edge arrows: the hero's glass circle, laid over the ends of the strip.
 *
 * Hidden until the shelf is hovered or something inside it takes focus, and
 * only on a device that can hover at all — on a phone the strip is scrolled
 * with a thumb and an arrow over the artwork would be two controls fighting
 * over the same corner.
 */
const ARROW_CLASS = cx(
  'absolute top-1/2 z-10 hidden -translate-y-1/2',
  '[@media(hover:hover)]:flex',
  'pointer-events-none opacity-0 transition-opacity duration-200',
  'group-hover/shelf:pointer-events-auto group-hover/shelf:opacity-100',
  'group-focus-within/shelf:pointer-events-auto group-focus-within/shelf:opacity-100',
  GLASS_CIRCLE,
  FOCUS_RING,
)

/**
 * A horizontal shelf: heading, one explanatory line, and a snapping scroller.
 *
 * The scrollbar is hidden and the items snap, so the rail stops between tiles
 * rather than halfway through one. Items are not wrapped in extra elements —
 * the shelf reaches into its children for `snap-start` and `shrink-0` instead,
 * so a caller can put a `<Link>`, an `<article>` or a `<li>` in it without
 * this component having an opinion.
 *
 * With a mouse there was nothing to grab: the wheel scrolls the page and only
 * Shift+wheel moved the rail (owner, 2026-09-13). So a mouse can now drag the
 * strip, and hovering it raises an arrow at each end that has somewhere to go.
 * Touch and pen keep the platform's own scrolling untouched.
 */
export function Shelf({ title, lede, action, children, className, scrollerClassName }: ShelfProps) {
  const scroller = useRef<HTMLDivElement | null>(null)
  const [edges, setEdges] = useState<Edges>(NO_OVERFLOW)
  const [dragging, setDragging] = useState(false)
  const drag = useRef<{
    pointerId: number
    startX: number
    startScroll: number
    moved: boolean
  } | null>(null)

  const measure = useCallback(() => {
    const el = scroller.current
    if (el === null) return
    const slack = el.scrollWidth - el.clientWidth
    const next: Edges =
      slack <= EDGE_SLACK_PX
        ? NO_OVERFLOW
        : {
            overflowing: true,
            atStart: el.scrollLeft <= EDGE_SLACK_PX,
            atEnd: el.scrollLeft >= slack - EDGE_SLACK_PX,
          }
    // Same answer, same object: this runs after every render, so a fresh
    // object every time would be a render loop rather than a measurement.
    setEdges((current) => (sameEdges(current, next) ? current : next))
  }, [])

  // After every render, because the tiles a page hands the shelf change with
  // the data and a `ResizeObserver` on the scroller never sees its content get
  // wider — only its own box.
  useEffect(measure)

  useEffect(() => {
    const el = scroller.current
    if (el === null) return

    // One measurement per frame: a scroll event fires far more often than a
    // pair of arrows can change.
    let frame = 0
    const schedule = () => {
      if (frame !== 0) return
      frame = requestAnimationFrame(() => {
        frame = 0
        measure()
      })
    }

    el.addEventListener('scroll', schedule, { passive: true })
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null
    observer?.observe(el)

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame)
      el.removeEventListener('scroll', schedule)
      observer?.disconnect()
    }
  }, [measure])

  const step = (direction: -1 | 1) => {
    const el = scroller.current
    if (el === null) return
    el.scrollBy?.({ left: direction * el.clientWidth * ARROW_STEP, behavior: 'smooth' })
  }

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    // Mouse only. A touch or a pen already drags the strip natively, and
    // taking those over would cost the platform's own inertia and rubber-band.
    if (event.pointerType !== 'mouse' || event.button !== 0) return
    const el = scroller.current
    if (el === null || el.scrollWidth - el.clientWidth <= EDGE_SLACK_PX) return

    drag.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startScroll: el.scrollLeft,
      moved: false,
    }
    // So a hand that leaves the strip mid-drag keeps scrolling it.
    el.setPointerCapture?.(event.pointerId)
  }

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const state = drag.current
    const el = scroller.current
    if (state === null || el === null || event.pointerId !== state.pointerId) return

    const travelled = event.clientX - state.startX
    if (!state.moved) {
      if (Math.abs(travelled) < DRAG_THRESHOLD_PX) return
      state.moved = true
      // The few pixels before the threshold may have started a selection.
      document.getSelection()?.removeAllRanges()
      setDragging(true)
    }
    el.scrollLeft = state.startScroll - travelled
  }

  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    const state = drag.current
    if (state === null || event.pointerId !== state.pointerId) return
    drag.current = null
    setDragging(false)
    scroller.current?.releasePointerCapture?.(event.pointerId)
    if (!state.moved) return

    // A real drag ends on a card, and that card is usually a link. Swallow the
    // click this release is about to produce — once, and only this one: the
    // timeout drops the listener if the release produced no click at all.
    const swallow = (click: MouseEvent) => {
      click.preventDefault()
      click.stopPropagation()
    }
    window.addEventListener('click', swallow, { capture: true, once: true })
    window.setTimeout(() => {
      window.removeEventListener('click', swallow, { capture: true })
    }, 0)
  }

  return (
    <section className={cx('min-w-0', className)}>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-[24px] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
            {title}
          </h2>
          {lede === undefined ? null : (
            <p className="mt-1.5 max-w-[66ch] text-[14px] text-[var(--arc-text-muted)]">{lede}</p>
          )}
        </div>
        {action}
      </div>

      <div className="group/shelf relative mt-[18px]">
        <div
          ref={scroller}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          // The tiles are links wrapping images, and both are natively
          // draggable: a few pixels in, Chrome started an HTML5 link drag and
          // sent `pointercancel`, so the rail never moved for a real mouse
          // (owner, in Chrome on dev, 2026-09-13). Refused here, once, for
          // everything in the strip — dragging a card out of Arc does nothing
          // anyone wants — rather than `draggable={false}` on every tile.
          onDragStart={(event) => {
            event.preventDefault()
          }}
          // Inline, because snap-mandatory fights a live drag and a class
          // would be arguing with `snap-x` over which one was written last.
          style={dragging ? { scrollSnapType: 'none' } : undefined}
          className={cx(
            'no-scrollbar flex snap-x snap-mandatory gap-6 overflow-x-auto pb-1',
            '[&>*]:shrink-0 [&>*]:snap-start',
            edges.overflowing && (dragging ? 'cursor-grabbing select-none' : 'cursor-grab'),
            scrollerClassName,
          )}
        >
          {children}
        </div>

        {edges.overflowing && !edges.atStart ? (
          <button
            type="button"
            aria-label="Scroll left"
            onClick={() => {
              step(-1)
            }}
            className={cx(ARROW_CLASS, 'left-0')}
          >
            <span aria-hidden>‹</span>
          </button>
        ) : null}

        {edges.overflowing && !edges.atEnd ? (
          <button
            type="button"
            aria-label="Scroll right"
            onClick={() => {
              step(1)
            }}
            className={cx(ARROW_CLASS, 'right-0')}
          >
            <span aria-hidden>›</span>
          </button>
        ) : null}
      </div>
    </section>
  )
}
