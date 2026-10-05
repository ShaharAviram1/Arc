import { QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useLogout } from '@/lib/auth'
import { createQueryClient } from '@/lib/queryClient'
import { isLogoutPending, recallUser, rememberUser } from '@/offline/cache'
import { setDownloads } from '@/offline/downloads'
import { TEST_USER } from '@/test/apiMock'
import { managerHarness } from '@/test/downloadFixtures'

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('logging out (FR-S9)', () => {
  it('with no network: signs out here at once and remembers to tell the server', async () => {
    await rememberUser(TEST_USER)
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Load failed'))),
    )
    const { result } = renderHook(() => useLogout(), { wrapper })

    act(() => {
      result.current.mutate()
    })

    await waitFor(async () => {
      expect(await isLogoutPending()).toBe(true)
    })
    expect(result.current.isError).toBe(false)
    expect(await recallUser()).toBeNull()
  })

  it('pauses a running download and hides every download at once', async () => {
    const harness = managerHarness()
    harness.manager.setOwner(TEST_USER.id)
    await harness.manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })
    setDownloads(harness.manager)
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))),
    )
    const { result } = renderHook(() => useLogout(), { wrapper })

    act(() => {
      result.current.mutate()
    })

    await waitFor(() => {
      expect(harness.worker.lastCommand()).toEqual({ cmd: 'pause', name: 'episode-9001.mp4' })
    })
    expect(harness.manager.getSnapshot()).toEqual({})
    expect(await isLogoutPending()).toBe(false)
  })
})
