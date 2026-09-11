import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { Recs } from '@/pages/Recs'
import {
  FRIEREN,
  FRIEREN_SEASON_2,
  FRIEREN_SPECIAL,
  REC_CASE_FRIEREN,
  REC_CASE_SPECIAL,
  REC_CONTINUATIONS,
  REC_RUN,
  REC_RUN_WITHOUT_CONTINUATIONS,
  RECS_PAGE,
  RECS_PAGE_ADMIN,
  RECS_PAGE_EMPTY,
  RECS_PAGE_UNCONFIGURED,
} from '@/test/animeFixtures'
import {
  callTo,
  jsonBodyOf,
  mockApi,
  requestsMade,
  TEST_ADMIN,
  TEST_USER,
  type MockRoutes,
} from '@/test/apiMock'
import type { User } from '@/lib/auth'

const RECS_PATH = 'GET /api/recs'
const RUNS_PATH = 'POST /api/recs/runs'

/** The page reads the viewer's role, so every render needs a session. */
function renderRecs(routes: MockRoutes, viewer: User = TEST_USER) {
  const fetchMock = mockApi({ 'GET /api/auth/me': { body: viewer }, ...routes })
  const router = createMemoryRouter(
    [
      { path: '/recs', element: <Recs /> },
      { path: '/anime/:id', element: <p>show page</p> },
    ],
    { initialEntries: ['/recs'] },
  )
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return fetchMock
}

/** The prompt a run sent, read back off the recorded POST. */
function promptSent(fetchMock: ReturnType<typeof mockApi>): unknown {
  return jsonBodyOf(callTo(fetchMock, '/api/recs/runs')).prompt
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Recs', () => {
  it('invites a first run when there is nothing stored yet', async () => {
    renderRecs({ [RECS_PATH]: { body: RECS_PAGE_EMPTY } })

    expect(await screen.findByText(/No picks yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Get picks' })).toBeEnabled()
    expect(screen.getByText('10 of 10 left today')).toBeInTheDocument()
    expect(screen.getByLabelText(/Mood/i)).toHaveValue('')
    // The two examples from the spec are offered, not explained.
    expect(screen.getByLabelText(/Mood/i)).toHaveAttribute(
      'placeholder',
      expect.stringContaining('like Mushishi'),
    )
  })

  it('renders the stored run: the mood it was for, and every pick with its case', async () => {
    renderRecs({ [RECS_PATH]: { body: RECS_PAGE } })

    expect(
      await screen.findByRole('heading', { name: /Picks for: something short and funny/ }),
    ).toBeInTheDocument()
    expect(screen.getByText(REC_CASE_FRIEREN)).toBeInTheDocument()
    expect(screen.getByText(REC_CASE_SPECIAL)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: FRIEREN.title.preferred })).toHaveAttribute(
      'href',
      `/anime/${String(FRIEREN.id)}`,
    )
    expect(screen.getByText('9 of 10 left today')).toBeInTheDocument()
    // One control, not two: "Get picks" is also how a run is refreshed.
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  it('gives each pick the 2:3 key visual and the case as body type', async () => {
    renderRecs({ [RECS_PATH]: { body: RECS_PAGE } })

    const case_ = await screen.findByText(REC_CASE_FRIEREN)
    // The case is the point of the card, so it gets body type, not a caption.
    expect(case_.className).toContain('text-[16px]')

    const cover = screen
      .getByRole('link', { name: FRIEREN.title.preferred })
      .closest('article')
      ?.querySelector('.aspect-\\[2\\/3\\]')
    expect(cover).not.toBeNull()
  })

  it('prefills the box from the newest run, so pressing Get picks refreshes it', async () => {
    const fetchMock = renderRecs({
      [RECS_PATH]: { body: RECS_PAGE },
      [RUNS_PATH]: { status: 201, body: { ...REC_RUN, id: 8 } },
    })

    expect(await screen.findByLabelText(/Mood/i)).toHaveValue(REC_RUN.prompt)

    await userEvent.click(screen.getByRole('button', { name: 'Get picks' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(RUNS_PATH)
    })
    // Untouched box, so the run is remade with the mood it was made for.
    expect(promptSent(fetchMock)).toBe(REC_RUN.prompt)
  })

  it('leaves the box empty when the newest run asked for nothing in particular', async () => {
    renderRecs({ [RECS_PATH]: { body: { ...RECS_PAGE, run: { ...REC_RUN, prompt: null } } } })

    expect(await screen.findByLabelText(/Mood/i)).toHaveValue('')
  })

  it('keeps what I am typing when the same run re-renders underneath me', async () => {
    renderRecs({
      [RECS_PATH]: { body: RECS_PAGE },
      [`PUT /api/list/${String(FRIEREN.id)}`]: {
        body: {
          anime_id: FRIEREN.id,
          status: 'planned',
          progress: 0,
          score: null,
          updated_at: '2026-09-10T09:00:00Z',
        },
      },
      'GET /api/list?status=planned': { body: [] },
    })

    const textarea = await screen.findByLabelText(/Mood/i)
    await userEvent.clear(textarea)
    await userEvent.type(textarea, 'half a thought')

    // Adding a pick to the list patches the recs cache and re-renders the
    // page. The run has not changed, so the box must not be re-filled.
    await userEvent.selectOptions(
      screen.getByLabelText(`List status for ${FRIEREN.title.preferred}`),
      'planned',
    )

    await waitFor(() => {
      expect(screen.getByLabelText(`List status for ${FRIEREN.title.preferred}`)).toHaveValue(
        'planned',
      )
    })
    expect(textarea).toHaveValue('half a thought')
  })

  it('resets the box to the new run once one completes', async () => {
    const fetchMock = renderRecs({
      [RECS_PATH]: { body: RECS_PAGE },
      [RUNS_PATH]: { status: 201, body: { ...REC_RUN, id: 8, prompt: 'like Mushishi' } },
    })

    const textarea = await screen.findByLabelText(/Mood/i)
    await userEvent.clear(textarea)
    await userEvent.type(textarea, 'like Mushishi')
    await userEvent.click(screen.getByRole('button', { name: 'Get picks' }))

    expect(
      await screen.findByRole('heading', { name: /Picks for: like Mushishi/ }),
    ).toBeInTheDocument()
    expect(promptSent(fetchMock)).toBe('like Mushishi')
    // A new run is a new subject; the box now shows what it was made for.
    expect(textarea).toHaveValue('like Mushishi')
  })

  it('offers a one-click list control on every pick, showing where each already is', async () => {
    renderRecs({ [RECS_PATH]: { body: RECS_PAGE } })

    const first = await screen.findByLabelText(`List status for ${FRIEREN.title.preferred}`)
    const second = screen.getByLabelText(`List status for ${FRIEREN_SPECIAL.title.preferred}`)

    expect(first).toHaveValue('')
    expect(second).toHaveValue('planned')
    expect(
      screen.getByRole('option', { name: 'Plan to watch', selected: true }),
    ).toBeInTheDocument()
    // FR-C6's caveat travels with the pick that carries it.
    expect(screen.getByTitle(/AniList is unavailable/i)).toHaveTextContent('via MAL')
  })

  it('adds a pick to the list and keeps the pick showing where it went', async () => {
    const fetchMock = renderRecs({
      [RECS_PATH]: { body: RECS_PAGE },
      [`PUT /api/list/${String(FRIEREN.id)}`]: {
        body: {
          anime_id: FRIEREN.id,
          status: 'planned',
          progress: 0,
          score: null,
          updated_at: '2026-09-10T09:00:00Z',
        },
      },
      'GET /api/list?status=planned': { body: [] },
    })

    const control = await screen.findByLabelText(`List status for ${FRIEREN.title.preferred}`)
    await userEvent.selectOptions(control, 'planned')

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`PUT /api/list/${String(FRIEREN.id)}`)
    })
    // The stored run is a snapshot the server will not recompute, so the
    // control has to keep the value rather than snap back to "Not on list".
    await waitFor(() => {
      expect(control).toHaveValue('planned')
    })
  })

  it('sends the trimmed mood and shows the run that comes back', async () => {
    const fetchMock = renderRecs({
      [RECS_PATH]: { body: RECS_PAGE_EMPTY },
      [RUNS_PATH]: { status: 201, body: REC_RUN },
    })

    await userEvent.type(await screen.findByLabelText(/Mood/i), '  like Mushishi  ')
    await userEvent.click(screen.getByRole('button', { name: 'Get picks' }))

    expect(await screen.findByText(REC_CASE_FRIEREN)).toBeInTheDocument()
    expect(promptSent(fetchMock)).toBe('like Mushishi')
    // One run spent, out of the ten the page was told about.
    expect(screen.getByText('9 of 10 left today')).toBeInTheDocument()
  })

  it('asks for nothing in particular when the mood is left empty', async () => {
    const fetchMock = renderRecs({
      [RECS_PATH]: { body: RECS_PAGE_EMPTY },
      [RUNS_PATH]: { status: 201, body: { ...REC_RUN, prompt: null } },
    })

    await userEvent.type(await screen.findByLabelText(/Mood/i), '   ')
    await userEvent.click(screen.getByRole('button', { name: 'Get picks' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(RUNS_PATH)
    })
    expect(promptSent(fetchMock)).toBeNull()
    expect(
      await screen.findByRole('heading', { name: /Picks for: no particular mood/ }),
    ).toBeInTheDocument()
  })

  it('says it is working while the call is out, since it takes half a minute', async () => {
    let release = (): void => undefined
    const pending = new Promise<void>((resolve) => {
      release = resolve
    })
    const fetchMock = mockApi({ [RECS_PATH]: { body: RECS_PAGE_EMPTY } })
    // The run hangs until the test lets it finish, so the pending label can be
    // observed rather than raced.
    const routed = fetchMock.getMockImplementation()
    fetchMock.mockImplementation((input, init) => {
      if ((init?.method ?? 'GET') === 'POST') {
        return pending.then(
          () =>
            new Response(JSON.stringify(REC_RUN), {
              status: 201,
              headers: { 'Content-Type': 'application/json' },
            }),
        )
      }
      return routed?.(input, init) ?? Promise.resolve(new Response(null, { status: 404 }))
    })

    const router = createMemoryRouter([{ path: '/recs', element: <Recs /> }], {
      initialEntries: ['/recs'],
    })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Get picks' }))

    const button = await screen.findByRole('button', { name: 'Finding picks…' })
    expect(button).toBeDisabled()

    release()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Get picks' })).toBeEnabled()
    })
  })

  it('says how long to wait when the daily limit is hit, without losing the picks', async () => {
    renderRecs({
      [RECS_PATH]: { body: RECS_PAGE },
      [RUNS_PATH]: {
        status: 429,
        body: { detail: 'daily limit reached', retry_after_seconds: 4 * 3600 },
      },
    })

    await userEvent.click(await screen.findByRole('button', { name: 'Get picks' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Daily limit reached; try again in about 4 hours.',
    )
    // Inline, not a full-page error: the stored run is still readable.
    expect(screen.getByText(REC_CASE_FRIEREN)).toBeInTheDocument()
  })

  it('re-asks for the allowance after a 429, so the counter stops contradicting it', async () => {
    const fetchMock = renderRecs({
      [RECS_PATH]: { body: RECS_PAGE },
      [RUNS_PATH]: {
        status: 429,
        body: { detail: 'daily limit reached', retry_after_seconds: 4 * 3600 },
      },
    })

    expect(await screen.findByText('9 of 10 left today')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Get picks' }))
    await screen.findByRole('alert')

    // The server disagrees about how many are left, so ours is the stale
    // number and the page goes and gets the real one.
    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === RECS_PATH)).toHaveLength(2)
    })
  })

  it('stops the mood at the length the server would reject, and counts up to it', async () => {
    renderRecs({ [RECS_PATH]: { body: RECS_PAGE_EMPTY } })

    const textarea = await screen.findByLabelText(/Mood/i)
    expect(textarea).toHaveAttribute('maxlength', '300')
    expect(screen.getByText('0/300')).toBeInTheDocument()

    await userEvent.type(textarea, 'like Mushishi')
    expect(screen.getByText('13/300')).toBeInTheDocument()
  })

  it('says how many candidates the picks were chosen from, and which model chose', async () => {
    renderRecs({ [RECS_PATH]: { body: RECS_PAGE } })

    expect(
      await screen.findByText(`Picked 2 of 38 candidates · ${String(REC_RUN.model)}`),
    ).toBeInTheDocument()
  })

  it('explains a server with no key and offers nothing to click', async () => {
    const fetchMock = renderRecs({ [RECS_PATH]: { body: RECS_PAGE_UNCONFIGURED } })

    expect(await screen.findByText(/need a model API key/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Mood/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Get picks' })).not.toBeInTheDocument()
    expect(requestsMade(fetchMock).filter((path) => path.startsWith('GET /api/recs'))).toEqual([
      RECS_PATH,
    ])
  })

  it('reports a page it could not load, with a way to ask again', async () => {
    const fetchMock = renderRecs({ [RECS_PATH]: { status: 500, body: { detail: 'boom' } } })

    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong. Try again.')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock).filter((path) => path === RECS_PATH)).toHaveLength(2)
    })
  })

  describe('the model chain', () => {
    it('shows an admin which models are in play, and which are resting', async () => {
      renderRecs({ [RECS_PATH]: { body: RECS_PAGE_ADMIN } }, TEST_ADMIN)

      const line = await screen.findByText(/^Models:/)
      expect(line).toHaveTextContent('gemini-3.5-flash ✓')
      // Out of quota is a model resting, not a model broken.
      expect(line).toHaveTextContent('gemini-2.5-flash (resting until tomorrow)')
      expect(line).not.toHaveTextContent(/error|failed|unavailable/i)
      // The provider is named only when the model id does not already say it.
      expect(line).toHaveTextContent('openai/gpt-5-mini via openrouter ✓')
    })

    it('does not show the chain to an ordinary viewer', async () => {
      // Even handed the field, a non-admin must not see it.
      renderRecs({ [RECS_PATH]: { body: RECS_PAGE_ADMIN } }, TEST_USER)

      expect(await screen.findByText('9 of 10 left today')).toBeInTheDocument()
      expect(screen.queryByText(/^Models:/)).not.toBeInTheDocument()
    })

    it('renders nothing at all for an empty chain', async () => {
      renderRecs({ [RECS_PATH]: { body: { ...RECS_PAGE, chain: [] } } }, TEST_ADMIN)

      expect(await screen.findByText('9 of 10 left today')).toBeInTheDocument()
      expect(screen.queryByText(/^Models:/)).not.toBeInTheDocument()
    })
  })

  describe('continuations', () => {
    it('lists what follows on from the list, with the reason for each', async () => {
      renderRecs({ [RECS_PATH]: { body: RECS_PAGE } })

      expect(
        await screen.findByRole('heading', { name: 'New in your franchises' }),
      ).toBeInTheDocument()
      expect(screen.getByText(/Sequels, movies and spin-offs/)).toBeInTheDocument()
      expect(screen.getByText(REC_CONTINUATIONS[0]?.because ?? '')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: FRIEREN_SEASON_2.title.preferred })).toHaveAttribute(
        'href',
        `/anime/${String(FRIEREN_SEASON_2.id)}`,
      )
    })

    it('stays hidden when the run has none', async () => {
      renderRecs({
        [RECS_PATH]: { body: { ...RECS_PAGE, run: { ...REC_RUN, continuations: [] } } },
      })

      expect(await screen.findByText(REC_CASE_FRIEREN)).toBeInTheDocument()
      expect(screen.queryByText('New in your franchises')).not.toBeInTheDocument()
    })

    it('treats a run stored without the key as having none', async () => {
      renderRecs({
        [RECS_PATH]: { body: { ...RECS_PAGE, run: REC_RUN_WITHOUT_CONTINUATIONS } },
      })

      expect(await screen.findByText(REC_CASE_FRIEREN)).toBeInTheDocument()
      expect(screen.queryByText('New in your franchises')).not.toBeInTheDocument()
    })

    it('still shows them when the run argued for no picks at all', async () => {
      renderRecs({
        [RECS_PATH]: { body: { ...RECS_PAGE, run: { ...REC_RUN, picks: [] } } },
      })

      expect(await screen.findByText(/came back with nothing/i)).toBeInTheDocument()
      expect(screen.getByRole('heading', { name: 'New in your franchises' })).toBeInTheDocument()
    })

    it('adds a continuation to the list and keeps it showing where it went', async () => {
      const fetchMock = renderRecs({
        [RECS_PATH]: { body: RECS_PAGE },
        [`PUT /api/list/${String(FRIEREN_SEASON_2.id)}`]: {
          body: {
            anime_id: FRIEREN_SEASON_2.id,
            status: 'planned',
            progress: 0,
            score: null,
            updated_at: '2026-09-10T09:00:00Z',
          },
        },
        'GET /api/list?status=planned': { body: [] },
      })

      const control = await screen.findByLabelText(
        `List status for ${FRIEREN_SEASON_2.title.preferred}`,
      )
      expect(control).toHaveValue('')
      await userEvent.selectOptions(control, 'planned')

      await waitFor(() => {
        expect(requestsMade(fetchMock)).toContain(`PUT /api/list/${String(FRIEREN_SEASON_2.id)}`)
      })
      await waitFor(() => {
        expect(control).toHaveValue('planned')
      })
    })
  })

  it('stops offering a run once the day is spent', async () => {
    renderRecs({ [RECS_PATH]: { body: { ...RECS_PAGE, remaining_today: 0 } } })

    expect(await screen.findByText('0 of 10 left today')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Get picks' })).toBeDisabled()
  })
})
