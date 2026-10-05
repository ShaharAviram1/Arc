import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { createQueryClient } from '@/lib/queryClient'
import { rememberUser } from '@/offline/cache'
import { setDownloads, type DownloadRecord } from '@/offline/downloads'
import { PLAY_INFO, PLAY_INFO_EPISODE_2 } from '@/test/animeFixtures'
import { mockApi, TEST_USER } from '@/test/apiMock'
import { downloadedRecord, managerHarness, recordEntry } from '@/test/downloadFixtures'

const TITLE = PLAY_INFO.anime.title.preferred

function install(records: DownloadRecord[]) {
  const harness = managerHarness({ initial: records.map(recordEntry) })
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
})
