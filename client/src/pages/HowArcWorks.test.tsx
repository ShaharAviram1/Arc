import { render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { HowArcWorks } from '@/pages/HowArcWorks'

/**
 * The page is static prose, so what is worth asserting is the *shape*: the
 * three sections a reviewer navigates by, the eight services the page promises
 * to account for, and the pipeline in the order it actually happens. A missing
 * service is the failure that matters — the page's whole claim is that it names
 * everything Arc talks to.
 */
function renderPage() {
  const router = createMemoryRouter(
    [
      { path: '/', element: <HowArcWorks /> },
      { path: '/schedule', element: <p>schedule page</p> },
      { path: '/list', element: <p>my list page</p> },
    ],
    { initialEntries: ['/'] },
  )
  render(<RouterProvider router={router} />)
}

const SERVICES = [
  'AniList',
  'MyAnimeList',
  'TMDB',
  'The offline catalogue',
  'Nyaa',
  'qBittorrent',
  'ffmpeg',
  'A language model',
]

const STEPS = [
  'Your list',
  'Schedule',
  'Search',
  'Download',
  'Match',
  'Prepare',
  'Watch',
  'Progress',
]

describe('How Arc works', () => {
  it('opens with the title and a lede written for somebody new to all of it', () => {
    renderPage()

    expect(screen.getByRole('heading', { level: 1, name: 'How Arc works' })).toBeInTheDocument()
    expect(screen.getByText(/Arc is a personal anime server/)).toBeInTheDocument()
  })

  it('has the three sections a reviewer reads in order', () => {
    renderPage()

    for (const name of ['From your list to your list', 'What Arc talks to', 'Where to look']) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
  })

  it('draws the pipeline as real, numbered text in the order it happens', () => {
    renderPage()

    const strip = screen.getByRole('list', { name: 'What Arc does, step by step' })
    const steps = within(strip).getAllByRole('listitem')
    expect(steps).toHaveLength(STEPS.length)
    for (const [index, name] of STEPS.entries()) {
      // The number comes from the markup as well as from the words, so a
      // screen reader is told "3 of 8" whatever the arrows are doing.
      expect(steps[index]).toHaveTextContent(`Step ${String(index + 1)}`)
      expect(steps[index]).toHaveTextContent(name)
    }
  })

  it('names every outside service, with a sentence each', () => {
    renderPage()

    const list = screen.getByLabelText('The services Arc talks to')
    for (const name of SERVICES) {
      expect(within(list).getByText(name)).toBeInTheDocument()
    }
    // The three that would be the reviewer's questions: where the video comes
    // from, what is written back, and who the model is.
    expect(screen.getByText(/behind a VPN with seeding switched off/)).toBeInTheDocument()
    expect(screen.getByText(/only when you do something/)).toBeInTheDocument()
    expect(screen.getByText(/shown, never applied/)).toBeInTheDocument()
  })

  it('states the three rules the rest of the app can be checked against', () => {
    renderPage()

    expect(
      screen.getByRole('heading', { level: 3, name: 'Three rules it does not break' }),
    ).toBeInTheDocument()
    expect(screen.getByText(/never a whole season/)).toBeInTheDocument()
    expect(screen.getByText(/goes to a review queue/)).toBeInTheDocument()
    expect(screen.getByText(/no automatic event ever lowers your progress/)).toBeInTheDocument()
  })

  it('points at the four places worth opening', () => {
    renderPage()

    expect(screen.getByRole('link', { name: 'Watch Now' })).toHaveAttribute('href', '/')
    expect(screen.getByRole('link', { name: 'Schedule' })).toHaveAttribute('href', '/schedule')
    expect(screen.getByRole('link', { name: 'A show page' })).toHaveAttribute('href', '/list')
    expect(screen.getByRole('link', { name: 'The player' })).toHaveAttribute('href', '/')
  })
})
