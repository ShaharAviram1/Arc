import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { CoverThumb } from '@/components/CoverThumb'
import { Artwork } from '@/components/ui/Artwork'
import { Button, PlayGlyph } from '@/components/ui/Button'
import { Chip } from '@/components/ui/Chip'
import { EmptyState } from '@/components/ui/EmptyState'
import { HeroFrame } from '@/components/ui/HeroFrame'
import { Row } from '@/components/ui/Row'
import { Segmented } from '@/components/ui/Segmented'
import { Shelf } from '@/components/ui/Shelf'
import { Skeleton } from '@/components/ui/Skeleton'

/** The progress strip, found by the element that actually draws the fill. */
function progressWidth(container: HTMLElement): string | undefined {
  const track = container.querySelector('.absolute.inset-x-0.bottom-0')
  return (track?.firstElementChild as HTMLElement | null)?.style.width
}

describe('Artwork', () => {
  it('renders the catalogue image when there is one', () => {
    const { container } = render(<Artwork url="https://cdn.example/frieren.jpg" />)

    const image = container.querySelector('img')
    expect(image).toHaveAttribute('src', 'https://cdn.example/frieren.jpg')
    // Decorative: the title it sits beside is what names the show.
    expect(image).toHaveAttribute('alt', '')
    expect(container.querySelector('.art-placeholder')).toBeNull()
  })

  it('keeps its shape with the striped placeholder when the cover is missing', () => {
    const { container } = render(<Artwork url={null} shape="key" />)

    expect(container.querySelector('img')).toBeNull()
    const placeholder = container.querySelector('.art-placeholder')
    expect(placeholder).not.toBeNull()
    // Hidden from assistive tech: "no cover" is not information.
    expect(placeholder).toHaveAttribute('aria-hidden', 'true')
    expect(container.firstElementChild).toHaveClass('aspect-[2/3]')
  })

  it('names the show when the artwork is the only thing that does', () => {
    render(<Artwork url={null} alt="Frieren" />)

    expect(screen.getByRole('img', { name: 'Frieren' })).toBeInTheDocument()
  })

  it('gives each shape its own ratio and radius', () => {
    const { container: still } = render(<Artwork url={null} shape="still" />)
    expect(still.firstElementChild).toHaveClass('aspect-[16/9]', 'rounded-art')

    const { container: hero } = render(<Artwork url={null} shape="hero" />)
    expect(hero.firstElementChild).toHaveClass('aspect-[21/9]', 'rounded-hero')

    // An episode still in a row is the same ratio at a tighter radius.
    const { container: row } = render(<Artwork url={null} shape="still" radius="still" />)
    expect(row.firstElementChild).toHaveClass('rounded-still')
  })

  it('draws the progress strip only for a part-watched episode', () => {
    const { container: unstarted } = render(<Artwork url={null} shape="still" />)
    expect(progressWidth(unstarted)).toBeUndefined()

    const { container: watched } = render(<Artwork url={null} shape="still" progress={0.59} />)
    expect(progressWidth(watched)).toBe('59%')
  })

  it('clamps a progress value the player could not have meant', () => {
    const { container } = render(<Artwork url={null} progress={1.4} />)

    expect(progressWidth(container)).toBe('100%')
  })

  it('loads lazily unless it is the image a page is built around', () => {
    const { container: shelf } = render(<Artwork url="https://cdn.example/a.jpg" />)
    expect(shelf.querySelector('img')).toHaveAttribute('loading', 'lazy')

    const { container: hero } = render(<Artwork url="https://cdn.example/a.jpg" eager />)
    expect(hero.querySelector('img')).toHaveAttribute('loading', 'eager')
    expect(hero.querySelector('img')).toHaveAttribute('decoding', 'async')
  })
})

describe('HeroFrame', () => {
  const BANNER = 'https://cdn.example/frieren-banner.jpg'
  const POSTER = 'https://cdn.example/frieren-large.jpg'

  /**
   * jsdom decodes nothing, so an image's intrinsic size has to be planted on
   * the element and the load announced by hand — which is exactly the pair of
   * facts the frame reads.
   */
  function load(image: HTMLImageElement, width: number, height: number): void {
    Object.defineProperty(image, 'naturalWidth', { value: width, configurable: true })
    Object.defineProperty(image, 'naturalHeight', { value: height, configurable: true })
    fireEvent.load(image)
  }

  function frameOf(container: HTMLElement): HTMLElement {
    return container.firstElementChild as HTMLElement
  }

  /** The off-frame copy of the banner, which is what reports its shape. */
  function probe(container: HTMLElement): HTMLImageElement {
    return container.querySelector(`img[src="${BANNER}"]`) as HTMLImageElement
  }

  /** The frame is one fixed 21:9 box; nothing may override it with a ratio. */
  function expectFixedFrame(container: HTMLElement): void {
    const frame = frameOf(container)
    expect(frame).toHaveClass('aspect-[21/9]')
    expect(frame.style.aspectRatio).toBe('')
  }

  it('fills the frame with a banner that is a 16:9 backdrop', () => {
    const { container } = render(
      <HeroFrame banner={BANNER} poster={POSTER}>
        <h1>Frieren</h1>
      </HeroFrame>,
    )

    // Until the shape is known the wash holds the frame, so nothing jumps.
    expect(container.querySelector('[data-hero-backdrop]')).toHaveAttribute('src', POSTER)

    load(probe(container), 1920, 1080)

    const images = container.querySelectorAll('img')
    expect(images).toHaveLength(1)
    expect(images[0]).toHaveAttribute('src', BANNER)
    // Above the fold by definition: it is not waiting for a scroll.
    expect(images[0]).toHaveAttribute('loading', 'eager')
    expect(images[0]).toHaveAttribute('decoding', 'async')

    // No poster hero: nothing is blurred, and the poster is not repeated.
    expect(container.querySelector('[data-hero-backdrop]')).toBeNull()
    expect(container.querySelector('[data-hero-poster]')).toBeNull()
    expect(screen.getByRole('heading', { name: 'Frieren' })).toBeInTheDocument()
    // 16:9 into 21:9 loses a little off the top and bottom, and the frame is
    // the same 21:9 box it would be for any other show.
    expectFixedFrame(container)
  })

  it('washes the poster across the frame and lays the crisp one beside the title', () => {
    const { container } = render(
      <HeroFrame banner={null} poster={POSTER}>
        <h1>The Apothecary Diaries</h1>
      </HeroFrame>,
    )

    // The wash: the same picture, blurred past recognition and darkened, so
    // its 230px origin stops being something a viewer can see.
    const backdrop = container.querySelector('[data-hero-backdrop]')
    expect(backdrop).toHaveAttribute('src', POSTER)
    expect(backdrop).toHaveAttribute('alt', '')
    expect(backdrop).toHaveAttribute('aria-hidden', 'true')
    expect(backdrop).toHaveAttribute('loading', 'eager')
    expect(backdrop).toHaveClass(
      'blur-[40px]',
      'scale-[1.15]',
      'brightness-[0.5]',
      'saturate-[1.2]',
    )

    // And the crisp poster, at its own 2:3 ratio. Never stretched to 21:9.
    const poster = container.querySelector('[data-hero-poster]') as HTMLElement
    expect(poster.querySelector('img')).toHaveAttribute('src', POSTER)
    expect(poster.firstElementChild).toHaveClass('aspect-[2/3]')

    // Left of the title block, not over it.
    const title = screen.getByRole('heading', { name: 'The Apothecary Diaries' })
    expect(poster.compareDocumentPosition(title)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(poster).not.toContainElement(title)
    expectFixedFrame(container)
  })

  it('frames the poster rather than cropping a 4.75:1 banner into the hero', () => {
    const { container } = render(<HeroFrame banner={BANNER} poster={POSTER} />)

    // AniList ships ~1900×400: object-cover in a 21:9 frame shows the middle
    // half of one, so the banner is not what fills the frame.
    load(probe(container), 1900, 400)

    expect(container.querySelector(`img[src="${BANNER}"]`)).toBeNull()
    expect(container.querySelector('[data-hero-backdrop]')).toHaveAttribute('src', POSTER)
    const poster = container.querySelector('[data-hero-poster]') as HTMLElement
    expect(poster.querySelector('img')).toHaveAttribute('src', POSTER)
    expectFixedFrame(container)
  })

  it('blurs the banner itself into the ground when there is no poster', () => {
    const { container } = render(<HeroFrame banner={BANNER} poster={null} />)

    load(probe(container), 1900, 400)

    // Too wide to read as a picture, fine as a colour — and it is the only
    // artwork the show has, so it is the ground rather than nothing.
    expect(container.querySelector('[data-hero-backdrop]')).toHaveAttribute('src', BANNER)
    // No crisp plate: there is no 2:3 poster to lay over it.
    expect(container.querySelector('[data-hero-poster]')).toBeNull()
    expectFixedFrame(container)
  })

  it('keeps the frame when the catalogue has no artwork at all', () => {
    const { container } = render(<HeroFrame banner={null} poster={null} />)

    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('.art-placeholder')).not.toBeNull()
    expectFixedFrame(container)
    // One striped box, not a striped box laid on a striped frame.
    expect(container.querySelector('[data-hero-poster]')).toBeNull()
    expect(container.querySelectorAll('.art-placeholder')).toHaveLength(1)
  })

  it('measures each banner against its own url, not the slide before it', () => {
    const { container, rerender } = render(<HeroFrame banner={BANNER} poster={POSTER} />)

    load(probe(container), 1920, 1080)
    expect(container.querySelectorAll('img')).toHaveLength(1)

    // The next slide is a different show: its banner has to be measured
    // before it can fill anything, whatever the last one measured.
    const other = 'https://cdn.example/apothecary-banner.jpg'
    rerender(<HeroFrame banner={other} poster={POSTER} />)
    expect(container.querySelector('[data-hero-backdrop]')).toHaveAttribute('src', POSTER)
    expectFixedFrame(container)
  })
})

describe('CoverThumb', () => {
  it('still renders a cover for the pages written before the design pass', () => {
    const { container } = render(<CoverThumb url="https://cdn.example/a.jpg" className="w-16" />)

    expect(container.querySelector('img')).toHaveAttribute('src', 'https://cdn.example/a.jpg')
    // No shape of its own: the caller's sizing is the whole geometry.
    expect(container.firstElementChild).toHaveClass('w-16')
    expect(container.firstElementChild?.className).not.toMatch(/aspect-/)
  })

  it('falls back to the placeholder when the catalogue has no cover', () => {
    const { container } = render(<CoverThumb url={null} />)

    expect(container.querySelector('.art-placeholder')).not.toBeNull()
  })
})

describe('Button', () => {
  it('is a non-submitting button unless it is told otherwise', () => {
    render(<Button>Episodes</Button>)

    expect(screen.getByRole('button', { name: 'Episodes' })).toHaveAttribute('type', 'button')
  })

  it('dresses the primary action in white and the danger one in its own colour', () => {
    render(
      <>
        <Button variant="primary" iconLeft={<PlayGlyph />}>
          Resume episode 12
        </Button>
        <Button variant="danger">Delete files</Button>
      </>,
    )

    expect(screen.getByRole('button', { name: 'Resume episode 12' })).toHaveClass(
      'bg-[var(--arc-action)]',
      'text-[var(--arc-action-ink)]',
    )
    expect(screen.getByRole('button', { name: 'Delete files' }).className).toContain('--arc-error')
  })

  it('keeps every control at or above the 44px floor', () => {
    render(
      <>
        <Button variant="primary">Play</Button>
        <Button variant="chip">Filter</Button>
      </>,
    )

    expect(screen.getByRole('button', { name: 'Play' })).toHaveClass('h-12')
    expect(screen.getByRole('button', { name: 'Filter' })).toHaveClass('h-11')
  })

  it('does not fire while disabled', async () => {
    const onClick = vi.fn()
    render(
      <Button disabled onClick={onClick}>
        Confirm
      </Button>,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('Chip', () => {
  it('says which filter is on', async () => {
    const onClick = vi.fn()
    render(
      <>
        <Chip active>All</Chip>
        <Chip onClick={onClick}>Fantasy</Chip>
      </>,
    )

    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true')
    const fantasy = screen.getByRole('button', { name: 'Fantasy' })
    expect(fantasy).toHaveAttribute('aria-pressed', 'false')

    await userEvent.click(fantasy)
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

describe('Segmented', () => {
  it('marks the chosen segment and reports the next one', async () => {
    const onChange = vi.fn()
    render(
      <Segmented
        label="Cour"
        value="c1"
        onChange={onChange}
        options={[
          { value: 'c1', label: 'Cour 1' },
          { value: 'c2', label: 'Cour 2' },
        ]}
      />,
    )

    const group = screen.getByRole('group', { name: 'Cour' })
    expect(group).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cour 1' })).toHaveAttribute('aria-pressed', 'true')

    await userEvent.click(screen.getByRole('button', { name: 'Cour 2' }))
    expect(onChange).toHaveBeenCalledWith('c2')
  })

  it('carries a count beside the label when it is given one', () => {
    render(
      <Segmented
        label="Review queue"
        value="pending"
        onChange={vi.fn()}
        options={[{ value: 'pending', label: 'Pending', hint: '3' }]}
      />,
    )

    expect(screen.getByRole('button', { name: /Pending/ })).toHaveTextContent('Pending3')
  })
})

describe('Skeleton', () => {
  it('announces the wait once and hides the blocks from assistive tech', () => {
    const { container } = render(<Skeleton shape="key" count={4} />)

    expect(screen.getByRole('status')).toHaveTextContent('Loading…')
    expect(container.querySelectorAll('.animate-arcpulse').length).toBeGreaterThanOrEqual(4)
    for (const block of container.querySelectorAll('.animate-arcpulse')) {
      expect(block).toHaveAttribute('aria-hidden', 'true')
    }
  })

  it('draws one hero however many it is asked for', () => {
    const { container } = render(<Skeleton shape="hero" count={5} />)

    expect(container.querySelectorAll('.animate-arcpulse')).toHaveLength(1)
  })

  it('says nothing when the page already has a live region', () => {
    render(<Skeleton shape="row" count={2} label={null} />)

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

describe('Shelf', () => {
  it('heads the section and puts its items in one scroller', () => {
    render(
      <Shelf title="Up Next" lede="Where you stopped.">
        <article>Episode 12</article>
      </Shelf>,
    )

    expect(screen.getByRole('heading', { name: 'Up Next', level: 2 })).toBeInTheDocument()
    expect(screen.getByText('Where you stopped.')).toBeInTheDocument()
    expect(screen.getByText('Episode 12').parentElement).toHaveClass('snap-x')
  })
})

describe('Row', () => {
  it('is a link when it goes somewhere and a button when it does something', async () => {
    const onClick = vi.fn()
    render(
      <MemoryRouter>
        <Row to="/anime/1">Frieren</Row>
        <Row onClick={onClick}>Mark watched</Row>
      </MemoryRouter>,
    )

    expect(screen.getByRole('link', { name: 'Frieren' })).toHaveAttribute('href', '/anime/1')
    await userEvent.click(screen.getByRole('button', { name: 'Mark watched' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

describe('EmptyState', () => {
  it('says what would fill the page and offers one way to fill it', () => {
    render(
      <EmptyState
        title="Nothing on your list yet"
        message="Add a show from Browse and Arc will fetch the next episodes."
        action={<Button variant="primary">Go to Browse</Button>}
      />,
    )

    expect(screen.getByText('Nothing on your list yet')).toBeInTheDocument()
    expect(
      screen.getByText('Add a show from Browse and Arc will fetch the next episodes.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Go to Browse' })).toBeInTheDocument()
  })
})
