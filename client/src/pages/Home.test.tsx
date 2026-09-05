import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Home } from '@/pages/Home'
import { createQueryClient } from '@/lib/queryClient'

function renderHome() {
  const client = createQueryClient()
  return render(
    <QueryClientProvider client={client}>
      <Home />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Home', () => {
  it('shows the API status when /api/health responds', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: 'ok', version: '0.1.0', env: 'dev' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    renderHome()

    expect(await screen.findByText('API: ok')).toBeInTheDocument()
  })

  it('shows "API: unreachable" when the API cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    renderHome()

    // The shared query defaults retry once with a ~1s backoff, so allow for that.
    expect(await screen.findByText('API: unreachable', {}, { timeout: 5000 })).toBeInTheDocument()
  })
})
