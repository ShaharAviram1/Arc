import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import type { PlayInfo } from '@/lib/playback'
import { createQueryClient } from '@/lib/queryClient'
import { setDownloads, type DownloadRecord } from '@/offline/downloads'
import { resetNetwork } from '@/offline/network'
import { setOutbox } from '@/offline/outbox'
import { PLAY_INFO } from '@/test/animeFixtures'
import { mockApi, TEST_USER } from '@/test/apiMock'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'
import { instances, resetHls } from '@/test/hlsMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

/**
 * A trip episode that lives only on the viewer's devices (M19 T6, FR-A12):
 * `/play` answers `offline_only` with no playlist. With no copy here the page
 * says to keep it offline first; with one, it plays the file — and a null
 * playlist never reaches a `<video>`.
 */

const SMALL = 'episode-9001-o.mp4'

const OFFLINE_ONLY: PlayInfo = {
  ...PLAY_INFO,
  episode: { ...PLAY_INFO.episode, state: 'not_wanted', trip_only: true },
  playlist_url: null,
  offline_only: true,
  resume_position: null,
}

function install(records: DownloadRecord[]) {
  const harness = managerHarness({ initial: records.map(recordEntry) })
  for (const record of records) harness.files.set(record.name, record.bytes)
  setDownloads(harness.manager)
  return harness
}

function renderPlayer(episodeId = 9001) {
  const router = createMemoryRouter(routes, { initialEntries: [`/watch/${String(episodeId)}`] })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

function pretendOpfs() {
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: { getDirectory: () => Promise.reject(new Error('not in tests')) },
  })
  vi.stubGlobal('Worker', class {})
}

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'storage')
  setDownloads(null)
  resetHls()
  setOutbox(null)
  resetNetwork()
})

describe('an offline-only trip episode in the player', () => {
  it('with no copy on this device, says to keep it offline first and sets no source', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/episodes/9001/play': { body: OFFLINE_ONLY },
    })
    pretendOpfs()
    install([])

    renderPlayer()

    expect(
      await screen.findByText(/This episode is only for your device — keep it offline first/),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to Downloads' })).toHaveAttribute(
      'href',
      '/downloads',
    )
    expect(document.querySelector('video')).toBeNull()
    expect(instances).toHaveLength(0)
  })

  it('with the copy on this device, plays it from the file', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/episodes/9001/play': { body: OFFLINE_ONLY },
      'POST /api/progress': {
        body: { completed: false, newly_completed: false, list_progress: null },
      },
    })
    pretendOpfs()
    install([
      {
        ...downloadedRecord(TEST_USER.id, PLAY_INFO, 210),
        name: SMALL,
        url: '/media/9001/offline.mp4',
        variant: 'small',
        tripId: 77,
        confirm: 'done',
      },
    ])

    renderPlayer()

    const video = await waitFor(() => {
      const found = document.querySelector('video')
      if (found === null || found.getAttribute('src') === null) throw new Error('no source yet')
      return found
    })
    expect(video.getAttribute('src')).toBe(`blob:${SMALL}`)
    expect(instances).toHaveLength(0)
    // The top bar's control knows the copy, even though the episode is not ready.
    expect(
      await screen.findByRole('button', { name: 'Episode 1 is on this device' }),
    ).toBeInTheDocument()
  })

  it('offers the device’s copy of the next episode when the server’s is not ready', async () => {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      // Episode 2 is not ready on the server (PLAY_INFO.next).
      'GET /api/episodes/9001/play': { body: PLAY_INFO },
    })
    pretendOpfs()
    const second: DownloadRecord = {
      ...downloadedRecord(TEST_USER.id, PLAY_INFO, 210),
      episodeId: 9002,
      name: 'episode-9002-o.mp4',
      variant: 'small',
      tripId: 77,
      snapshot: {
        ...downloadedRecord(TEST_USER.id).snapshot,
        episode: { ...PLAY_INFO.episode, id: 9002, number: 2 },
      },
    }
    install([second])

    renderPlayer()

    const next = await screen.findByRole('link', { name: 'Next episode 2' })
    expect(next).toHaveAttribute('href', '/watch/9002')
  })
})
