import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ListStatusControl } from '@/components/ListStatusControl'
import { createQueryClient } from '@/lib/queryClient'
import { FRIEREN } from '@/test/animeFixtures'
import { mockApi, requestsMade } from '@/test/apiMock'

function renderControl(status: 'watching' | null = null) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <ListStatusControl animeId={FRIEREN.id} status={status} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ListStatusControl', () => {
  it('is disabled while the change is in flight', async () => {
    let release: ((response: Response) => void) | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            release = resolve
          }),
      ),
    )

    renderControl()
    const select = screen.getByLabelText('List status')
    await userEvent.setup().selectOptions(select, 'watching')

    await waitFor(() => {
      expect(select).toBeDisabled()
    })

    release?.(new Response(null, { status: 204 }))
  })

  it('shows an inline error when the server fails the change', async () => {
    mockApi({ [`PUT /api/list/${FRIEREN.id}`]: { status: 500, body: { detail: 'boom' } } })

    renderControl()
    await userEvent.setup().selectOptions(screen.getByLabelText('List status'), 'watching')

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save that change')
    expect(screen.getByLabelText('List status')).toBeEnabled()
  })

  it('deletes the entry when the show is taken off the list', async () => {
    const fetchMock = mockApi({ [`DELETE /api/list/${FRIEREN.id}`]: { status: 204 } })

    renderControl('watching')
    await userEvent.setup().selectOptions(screen.getByLabelText('List status'), '')

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toEqual([`DELETE /api/list/${FRIEREN.id}`])
    })
  })

  it('sends nothing when a show that is already off the list is set to "Not on list"', async () => {
    const fetchMock = mockApi({})

    renderControl(null)
    await userEvent.setup().selectOptions(screen.getByLabelText('List status'), '')

    expect(requestsMade(fetchMock)).toHaveLength(0)
  })
})
