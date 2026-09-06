import { QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useCatalogStatus, type CatalogStatus } from '@/lib/catalog'
import { createQueryClient } from '@/lib/queryClient'
import { mockApi, requestsMade } from '@/test/apiMock'

/** AniList tripped, MAL answering: the shape the fallback notice comes from. */
const STATUS: CatalogStatus = {
  sources: {
    anilist: {
      state: 'open',
      healthy_at: '2026-09-06T08:00:00Z',
      failed_at: '2026-09-06T09:00:00Z',
      reason: 'api temporarily disabled upstream',
      configured: true,
    },
    mal: {
      state: 'closed',
      healthy_at: '2026-09-06T09:05:00Z',
      failed_at: null,
      reason: null,
      configured: true,
    },
  },
  active: 'mal',
}

function Wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: createQueryClient() }, children)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useCatalogStatus', () => {
  it('fetches the admin catalogue status and returns it typed', async () => {
    const fetchMock = mockApi({ 'GET /api/catalog/status': { body: STATUS } })

    const { result } = renderHook(() => useCatalogStatus(), { wrapper: Wrapper })

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true)
    })
    expect(requestsMade(fetchMock)).toContain('GET /api/catalog/status')
    expect(result.current.data).toEqual(STATUS)
    // The fields the admin panel will actually branch on.
    expect(result.current.data?.active).toBe('mal')
    expect(result.current.data?.sources.anilist?.reason).toBe('api temporarily disabled upstream')
    expect(result.current.data?.sources.mal?.configured).toBe(true)
  })
})
