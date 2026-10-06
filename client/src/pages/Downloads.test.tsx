import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import { rememberUser } from '@/offline/cache'
import { setDownloads, type DownloadRecord } from '@/offline/downloads'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { mockApi, requestsMade, TEST_USER } from '@/test/apiMock'
import { downloadedRecord, managerHarness, recordEntry, smallCopy } from '@/test/downloadFixtures'

const TITLE = PLAY_INFO.anime.title.preferred

function install(
  records: DownloadRecord[],
  options: Omit<Parameters<typeof managerHarness>[0], 'initial'> = {},
) {
  const harness = managerHarness({ ...options, initial: records.map(recordEntry) })
  for (const record of records) harness.files.set(record.name, record.bytes)
  setDownloads(harness.manager)
  return harness
}

function renderApp(path = '/downloads') {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

/** Airplane mode: every request fails without a response. */
function noNetwork() {
  const fetchMock = vi.fn(() => Promise.reject(new TypeError('Load failed')))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/**
 * A browser that can keep episodes: OPFS and a Worker constructor. jsdom has
 * neither, which is the "hide it, say why" case the page also has to handle.
 */
function pretendOpfs() {
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: {
      getDirectory: () => Promise.reject(new Error('not in tests')),
      estimate: () => Promise.resolve({ usage: 500_000_000, quota: 2_000_000_000 }),
      persisted: () => Promise.resolve(true),
    },
  })
  vi.stubGlobal('Worker', class {})
}

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'storage')
})

describe('Downloads (FR-S9)', () => {
  it('renders in full when Arc is launched with no network', async () => {
    await rememberUser(TEST_USER)
    install([
      downloadedRecord(TEST_USER.id, PLAY_INFO, 412_000_000),
      {
        ...downloadedRecord(TEST_USER.id, PLAY_INFO_EPISODE_2, 100),
        state: 'downloading',
        total: 400,
      },
    ])
    noNetwork()

    renderApp()

    expect(await screen.findByRole('heading', { name: 'Downloads' })).toBeInTheDocument()
    expect(screen.queryByText('Can’t reach Arc')).not.toBeInTheDocument()
    const play = await screen.findByRole('link', { name: `Play ${TITLE} episode 1` })
    expect(play).toHaveAttribute('href', '/watch/9001')
    expect(screen.getByText(/On this device · 393 MB/)).toBeInTheDocument()
    // The second was mid-flight when the app closed: it comes back paused.
    expect(screen.getByRole('button', { name: `Resume ${TITLE} episode 2` })).toBeInTheDocument()
    expect(
      screen.getByRole('progressbar', { name: `Downloading ${TITLE} episode 2` }),
    ).toHaveAttribute('aria-valuenow', '25')
    expect(screen.getByText(/not on screen/)).toBeInTheDocument()
    expect(screen.getByText(/Removing Arc from your Home Screen deletes/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Storage' })).toBeInTheDocument()
  })

  it('reports the storage figures and whether they are persistent', async () => {
    pretendOpfs()
    await rememberUser(TEST_USER)
    install([downloadedRecord(TEST_USER.id, PLAY_INFO, 412_000_000)])
    noNetwork()

    renderApp()

    expect(await screen.findByText(/of the 1\.9 GB this browser allows/)).toBeInTheDocument()
    expect(screen.getByText(/Storage is persistent/)).toBeInTheDocument()
  })

  it('says why there is nothing to keep on a browser that cannot', async () => {
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    install([])

    renderApp()

    expect(await screen.findByText(/This browser can’t keep episodes/)).toBeInTheDocument()
  })

  it('says it is offline on other pages and points here', async () => {
    await rememberUser(TEST_USER)
    install([])
    noNetwork()

    renderApp('/list')

    const strip = await screen.findByText(/You’re offline/)
    expect(within(strip.closest('p') as HTMLElement).getByRole('link')).toHaveAttribute(
      'href',
      '/downloads',
    )
  })

  it('deletes only after a confirmation in the page', async () => {
    pretendOpfs()
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    const { files, removed } = install([downloadedRecord(TEST_USER.id)])
    expect(files.size).toBe(1)
    const user = userEvent.setup()

    renderApp()
    await user.click(await screen.findByRole('button', { name: `Delete ${TITLE} episode 1` }))

    const confirm = screen.getByRole('group', {
      name: `Delete ${TITLE} episode 1 from this device?`,
    })
    expect(within(confirm).getByText(/Your progress is kept/)).toBeInTheDocument()
    await user.click(within(confirm).getByRole('button', { name: 'Keep it' }))
    expect(removed).toEqual([])

    await user.click(screen.getByRole('button', { name: `Delete ${TITLE} episode 1` }))
    await user.click(screen.getByRole('button', { name: 'Delete download' }))

    expect(await screen.findByText(/Nothing is downloaded/)).toBeInTheDocument()
    expect(removed).toEqual(['episode-9001.mp4'])
  })

  it('shows nothing another account downloaded on this device', async () => {
    pretendOpfs()
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    install([downloadedRecord(TEST_USER.id + 1)])

    renderApp()

    expect(await screen.findByText(/Nothing is downloaded/)).toBeInTheDocument()
    expect(screen.queryByText(TITLE)).not.toBeInTheDocument()
  })

  it('pauses and resumes a download from its row', async () => {
    pretendOpfs()
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    const { manager, worker } = install([])
    const user = userEvent.setup()
    renderApp()
    await screen.findByText(/Nothing is downloaded/)
    await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })

    await user.click(await screen.findByRole('button', { name: `Pause ${TITLE} episode 1` }))
    expect(worker.lastCommand()).toEqual({ cmd: 'pause', name: 'episode-9001.mp4' })

    await user.click(screen.getByRole('button', { name: `Resume ${TITLE} episode 1` }))
    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', name: 'episode-9001.mp4' })
  })

  it('says quietly which copy each episode is (M19)', async () => {
    pretendOpfs()
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    install([
      {
        ...downloadedRecord(TEST_USER.id, PLAY_INFO, 210 * 1024 * 1024),
        name: 'episode-9001-o.mp4',
        url: smallCopy(9001).url,
        variant: 'small',
      },
      downloadedRecord(TEST_USER.id, PLAY_INFO_EPISODE_2, 700 * 1024 * 1024),
    ])

    renderApp()

    expect(await screen.findByText(/On this device · 210 MB · smaller copy/)).toBeInTheDocument()
    expect(screen.getByText(/On this device · 700 MB · full size/)).toBeInTheDocument()
  })

  it('lists a copy the server is still making as "Preparing on the server"', async () => {
    pretendOpfs()
    mockApi({ 'GET /api/auth/me': { body: TEST_USER } })
    const waiting: DownloadRecord = {
      ...downloadedRecord(TEST_USER.id),
      name: 'episode-9001-o.mp4',
      url: null,
      variant: 'small',
      fullUrl: '/media/9001/episode.mp4',
      state: 'preparing',
      serverProgress: null,
      bytes: 0,
      total: 0,
      etag: null,
    }
    install([waiting], {
      pollCopy: () =>
        Promise.resolve({
          state: 'preparing',
          progress: 0.4,
          size: null,
          url: null,
          codecs: 'avc1.640028',
        }),
    })

    renderApp()

    expect(await screen.findByText(/Preparing on the server · 40%/)).toBeInTheDocument()
    expect(
      screen.getByRole('progressbar', { name: `Preparing ${TITLE} episode 1 on the server` }),
    ).toHaveAttribute('aria-valuenow', '40')
    expect(screen.getByRole('button', { name: `Pause ${TITLE} episode 1` })).toBeInTheDocument()
    expect(screen.queryByText(/smaller copy/)).not.toBeInTheDocument()
  })
})

describe('a trip on the Downloads page (M19 T6)', () => {
  const TRIP_ID = 77

  function tripRecords(): DownloadRecord[] {
    const first: DownloadRecord = {
      ...downloadedRecord(TEST_USER.id, PLAY_INFO, 100_000_000),
      name: 'episode-9001-o.mp4',
      variant: 'small',
      tripId: TRIP_ID,
      confirm: 'pending',
    }
    const second: DownloadRecord = {
      ...downloadedRecord(TEST_USER.id, PLAY_INFO_EPISODE_2, 25_000_000),
      name: 'episode-9002-o.mp4',
      variant: 'small',
      tripId: TRIP_ID,
      state: 'downloading',
      total: 100_000_000,
    }
    return [first, second]
  }

  const CURRENT = {
    id: TRIP_ID,
    anime_id: PLAY_INFO.anime.id,
    anime_title: TITLE,
    first_number: 1,
    last_number: 2,
    count: 2,
    state: 'active',
    created_at: '2026-10-06T08:00:00Z',
    deadline_at: '2026-10-20T08:00:00Z',
    episodes: [],
  }

  it('groups the trip’s episodes under one heading with their total and counts', async () => {
    pretendOpfs()
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/trips/current': { body: null },
    })
    // The server has not answered the confirmation yet.
    install(tripRecords(), { confirmDelivered: () => new Promise(() => undefined) })

    renderApp()

    const group = await screen.findByRole('region', { name: `Trip · ${TITLE}` })
    expect(
      within(group).getByRole('heading', { name: `Trip · ${TITLE} · 2 episodes · 191 MB` }),
    ).toBeInTheDocument()
    // The download that was running when Arc closed comes back paused.
    expect(within(group).getByText('1 on this device · 1 paused')).toBeInTheDocument()
    expect(within(group).getByText(/telling Arc it arrived/)).toBeInTheDocument()
    // Not the active trip (the server says there is none): nothing to cancel.
    expect(within(group).queryByRole('button', { name: 'Cancel trip' })).not.toBeInTheDocument()
  })

  it('keeps episodes outside a trip in their own list', async () => {
    pretendOpfs()
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, 'GET /api/trips/current': { body: null } })
    const [first] = tripRecords()
    install([first as DownloadRecord, downloadedRecord(TEST_USER.id, PLAY_INFO_EPISODE_2, 700)])

    renderApp()

    const group = await screen.findByRole('region', { name: `Trip · ${TITLE}` })
    expect(within(group).getAllByRole('button', { name: /^Delete / })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /^Delete / })).toHaveLength(2)
  })

  it('cancels the active trip from its group, after a confirmation in the page', async () => {
    pretendOpfs()
    const fetchMock = mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/trips/current': { body: CURRENT },
      [`DELETE /api/trips/${String(TRIP_ID)}`]: { status: 204 },
    })
    install(tripRecords())
    const user = userEvent.setup()

    renderApp()

    const group = await screen.findByRole('region', { name: `Trip · ${TITLE}` })
    await user.click(await within(group).findByRole('button', { name: 'Cancel trip' }))
    const ask = within(group).getByRole('group', { name: 'Cancel this trip?' })
    await user.click(within(ask).getByRole('button', { name: 'Cancel trip' }))

    await waitFor(() => {
      expect(requestsMade(fetchMock)).toContain(`DELETE /api/trips/${String(TRIP_ID)}`)
    })
    // What is on the device stays.
    expect(screen.getByRole('button', { name: `Delete ${TITLE} episode 1` })).toBeInTheDocument()
  })

  it('tells the server when a trip episode is deleted from the device', async () => {
    pretendOpfs()
    mockApi({ 'GET /api/auth/me': { body: TEST_USER }, 'GET /api/trips/current': { body: null } })
    const releaseDelivered = vi.fn(() => Promise.resolve(null))
    const [first] = tripRecords()
    install([{ ...(first as DownloadRecord), confirm: 'done' }], { releaseDelivered })
    const user = userEvent.setup()

    renderApp()
    await user.click(await screen.findByRole('button', { name: `Delete ${TITLE} episode 1` }))
    await user.click(screen.getByRole('button', { name: 'Delete download' }))

    await waitFor(() => {
      expect(releaseDelivered).toHaveBeenCalledWith(TRIP_ID, 9001)
    })
  })
})
