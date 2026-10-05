import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { OfflineButton, OfflineReason } from '@/components/OfflineButton'
import type { EpisodeOut } from '@/lib/anime'
import { setDownloads } from '@/offline/downloads'
import { TEST_USER } from '@/test/apiMock'
import {
  downloadedRecord,
  managerHarness,
  recordEntry,
  type ManagerHarness,
} from '@/test/downloadFixtures'

const EPISODE: Pick<EpisodeOut, 'id' | 'number' | 'state' | 'download_url'> = {
  id: 9001,
  number: 1,
  state: 'ready',
  download_url: '/media/9001/episode.mp4',
}
const FILE = 'episode-9001.mp4'

/** A browser that can: OPFS and a Worker constructor (jsdom has neither). */
function pretendOpfs() {
  Object.defineProperty(navigator, 'storage', {
    configurable: true,
    value: { getDirectory: () => Promise.reject(new Error('not in tests')) },
  })
  vi.stubGlobal('Worker', class {})
}

async function install(
  options: Parameters<typeof managerHarness>[0] = {},
): Promise<ManagerHarness> {
  const harness = managerHarness(options)
  harness.manager.setOwner(TEST_USER.id)
  await harness.manager.hydrate()
  setDownloads(harness.manager)
  return harness
}

function renderButton(props: Partial<Parameters<typeof OfflineButton>[0]> = {}) {
  return render(
    <MemoryRouter>
      <OfflineButton episode={EPISODE} {...props} />
      <OfflineReason episodeId={EPISODE.id} />
    </MemoryRouter>,
  )
}

/** The determinate ring's value, read off its progressbar. */
function ringValue(): string | null {
  return screen
    .getByRole('progressbar', { name: 'Download of episode 1' })
    .getAttribute('aria-valuenow')
}

function liveText(): string {
  return document.querySelector('[aria-live="polite"]')?.textContent ?? ''
}

beforeEach(() => {
  pretendOpfs()
})

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'storage')
  setDownloads(null)
})

describe('OfflineButton (FR-S9, owner 2026-10-05)', () => {
  it('offers to keep the episode, and a tap starts the download', async () => {
    const { worker } = await install()
    const user = userEvent.setup()
    renderButton()

    const button = screen.getByRole('button', { name: 'Keep episode 1 offline' })
    expect(button).toHaveAttribute('title', 'Download into Arc to watch without a connection')
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    await user.click(button)

    expect(
      await screen.findByRole('button', { name: 'Downloading episode 1, 0% — pause' }),
    ).toBeInTheDocument()
    expect(worker.lastCommand()).toMatchObject({ cmd: 'download', url: '/media/9001/episode.mp4' })
  })

  it('draws the progress as a ring and says the percentage in its name', async () => {
    const { manager, worker } = await install()
    renderButton()
    await act(async () => {
      await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })
    })

    act(() => {
      worker.emit({ type: 'progress', name: FILE, offset: 84, total: 200, etag: null })
    })
    expect(
      screen.getByRole('button', { name: 'Downloading episode 1, 42% — pause' }),
    ).toBeInTheDocument()
    expect(ringValue()).toBe('42')
    const announced = liveText()
    expect(announced).toBe('Downloading episode 1.')

    // The live region speaks per state, not per percent.
    act(() => {
      worker.emit({ type: 'progress', name: FILE, offset: 120, total: 200, etag: null })
    })
    expect(ringValue()).toBe('60')
    expect(liveText()).toBe(announced)
  })

  it('pauses on a tap while downloading, shows why, and resumes on the next', async () => {
    const { manager, worker } = await install()
    await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })
    worker.emit({ type: 'progress', name: FILE, offset: 50, total: 200, etag: null })
    const user = userEvent.setup()
    renderButton()

    await user.click(screen.getByRole('button', { name: 'Downloading episode 1, 25% — pause' }))
    const paused = screen.getByRole('button', {
      name: 'Download of episode 1 paused at 25% — resume',
    })
    expect(paused).toHaveAttribute('title', 'Paused.')
    expect(ringValue()).toBe('25')
    // The reason, as a line, where the caller has room for one.
    expect(screen.getByText('Paused.')).toBeInTheDocument()

    await user.click(paused)
    expect(manager.record(9001)?.state).toMatch(/queued|downloading/)
  })

  it('reads as queued while another download runs, and a tap takes it out of the queue', async () => {
    const { manager } = await install()
    await manager.start({ episodeId: 9002, url: '/media/9002/episode.mp4' })
    await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })
    const user = userEvent.setup()
    renderButton()

    const queued = screen.getByRole('button', { name: 'Episode 1 is queued to download — pause' })
    expect(queued).toHaveAttribute('data-state', 'queued')
    await user.click(queued)
    expect(manager.record(9001)?.state).toBe('paused')
  })

  it('marks a failed download, says why, and a tap tries again', async () => {
    const { manager, worker } = await install()
    await manager.start({ episodeId: 9001, url: '/media/9001/episode.mp4' })
    worker.emit({ type: 'failed', name: FILE, offset: 9, code: 'quota', reason: 'x' })
    const user = userEvent.setup()
    renderButton()

    const failed = screen.getByRole('button', { name: 'Download of episode 1 stopped — try again' })
    expect(screen.getByRole('alert')).toHaveTextContent(/out of space/)
    await user.click(failed)
    expect(manager.record(9001)?.state).toMatch(/queued|downloading/)
  })

  it('says so when the download could not even start, and a tap tries again', async () => {
    await install()
    const user = userEvent.setup()
    // No payload for this episode in the harness, so `start` rejects.
    render(
      <MemoryRouter>
        <OfflineButton
          episode={{ ...EPISODE, id: 4242, download_url: '/media/4242/episode.mp4' }}
        />
      </MemoryRouter>,
    )

    await user.click(screen.getByRole('button', { name: 'Keep episode 1 offline' }))
    expect(
      await screen.findByRole('button', {
        name: 'Could not start keeping episode 1 offline — try again',
      }),
    ).toBeInTheDocument()
  })

  describe('on this device', () => {
    async function installDownloaded(): Promise<ManagerHarness> {
      const harness = managerHarness({
        initial: [recordEntry(downloadedRecord(TEST_USER.id, undefined, 1000))],
      })
      harness.files.set(FILE, 1000)
      harness.manager.setOwner(TEST_USER.id)
      await harness.manager.hydrate()
      setDownloads(harness.manager)
      return harness
    }

    it('opens a menu in the page with the size, Remove and Downloads', async () => {
      await installDownloaded()
      const user = userEvent.setup()
      renderButton()

      const button = screen.getByRole('button', { name: 'Episode 1 is on this device' })
      expect(button).toHaveAttribute('aria-expanded', 'false')
      await user.click(button)

      expect(button).toHaveAttribute('aria-expanded', 'true')
      const menu = screen.getByRole('group', { name: 'Episode 1 on this device' })
      expect(menu).toHaveTextContent('On this device · 1000 B')
      expect(within(menu).getByRole('link', { name: 'Go to Downloads' })).toHaveAttribute(
        'href',
        '/downloads',
      )
      expect(within(menu).getByRole('button', { name: 'Remove from this device' })).toBeEnabled()
    })

    it('removes the copy from the device, and offers to keep it again', async () => {
      const { manager, removed } = await installDownloaded()
      const confirmSpy = vi.spyOn(window, 'confirm')
      const user = userEvent.setup()
      renderButton()

      await user.click(screen.getByRole('button', { name: 'Episode 1 is on this device' }))
      await user.click(screen.getByRole('button', { name: 'Remove from this device' }))

      await waitFor(() => {
        expect(manager.record(9001)).toBeUndefined()
      })
      expect(removed).toEqual([FILE])
      expect(confirmSpy).not.toHaveBeenCalled()
      expect(
        await screen.findByRole('button', { name: 'Keep episode 1 offline' }),
      ).toBeInTheDocument()
    })

    it('will not remove the copy the player is playing, and says why', async () => {
      const { manager } = await installDownloaded()
      const user = userEvent.setup()
      renderButton({ variant: 'player', playingFromDevice: true })

      await user.click(screen.getByRole('button', { name: 'Episode 1 is on this device' }))
      const remove = screen.getByRole('button', { name: 'Remove from this device' })
      expect(remove).toBeDisabled()
      expect(remove).toHaveAccessibleDescription(
        'Playing from this copy. Remove it after you leave the player.',
      )
      expect(manager.record(9001)?.state).toBe('downloaded')
    })

    it('puts the menu away on Escape and on a tap elsewhere, and tells the caller', async () => {
      await installDownloaded()
      const onMenuOpenChange = vi.fn()
      const user = userEvent.setup()
      renderButton({ onMenuOpenChange })

      const button = screen.getByRole('button', { name: 'Episode 1 is on this device' })
      await user.click(button)
      expect(onMenuOpenChange).toHaveBeenLastCalledWith(true)
      await user.keyboard('{Escape}')
      expect(screen.queryByRole('group')).not.toBeInTheDocument()
      expect(onMenuOpenChange).toHaveBeenLastCalledWith(false)

      await user.click(button)
      fireEvent.pointerDown(document.body)
      expect(screen.queryByRole('group')).not.toBeInTheDocument()
    })
  })

  describe('renders nothing', () => {
    it('for an episode that is not ready', async () => {
      await install()
      renderButton({ episode: { ...EPISODE, state: 'preparing' } })
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
    })

    it('for an episode with no file route', async () => {
      await install()
      renderButton({ episode: { ...EPISODE, download_url: null } })
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
    })

    it('on a browser with no OPFS or no workers', async () => {
      await install()
      Reflect.deleteProperty(navigator, 'storage')
      renderButton()
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
    })
  })
})
