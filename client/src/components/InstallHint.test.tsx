import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '@/app/router'
import { INSTALL_HINT_KEY, InstallHint } from '@/components/InstallHint'
import { createQueryClient } from '@/lib/queryClient'
import { mockApi, TEST_USER } from '@/test/apiMock'
import { resetDevice, stubDevice, UA } from '@/test/device'

const HINT = { name: 'Install Arc' }

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  resetDevice()
  window.localStorage.clear()
})

describe('InstallHint', () => {
  it('tells iPad Safari where Add to Home Screen is', () => {
    stubDevice({ ua: UA.ipadSafari, touchPoints: 5 })
    render(<InstallHint />)

    const hint = screen.getByRole('note', HINT)
    expect(hint).toHaveTextContent('in Safari, then Add to Home Screen')
  })

  it('sends Chrome on iOS to its address bar instead', () => {
    stubDevice({ ua: UA.ipadChrome, touchPoints: 5 })
    render(<InstallHint />)

    expect(screen.getByRole('note', HINT)).toHaveTextContent('Chrome’s address bar')
  })

  it.each([
    ['a desktop', { ua: UA.windowsChrome }],
    ['a Mac', { ua: UA.macSafari }],
    ['Firefox on iOS', { ua: UA.iphoneFirefox, touchPoints: 5 }],
    ['the installed app', { ua: UA.ipadSafari, touchPoints: 5, standalone: true }],
  ])('says nothing on %s', (_name, device) => {
    stubDevice(device)
    render(<InstallHint />)

    expect(screen.queryByRole('note', HINT)).not.toBeInTheDocument()
  })

  it('goes away when dismissed and stays away', async () => {
    stubDevice({ ua: UA.iphoneSafari, touchPoints: 5 })
    const user = userEvent.setup()
    const { unmount } = render(<InstallHint />)

    await user.click(screen.getByRole('button', { name: 'Dismiss' }))

    expect(screen.queryByRole('note', HINT)).not.toBeInTheDocument()
    expect(window.localStorage.getItem(INSTALL_HINT_KEY)).toBe('1')

    unmount()
    render(<InstallHint />)
    expect(screen.queryByRole('note', HINT)).not.toBeInTheDocument()
  })

  it('still renders, and still dismisses, when storage throws', async () => {
    stubDevice({ ua: UA.iphoneSafari, touchPoints: 5 })
    const blocked = () => {
      throw new DOMException('blocked', 'SecurityError')
    }
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(blocked)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(blocked)
    try {
      const user = userEvent.setup()
      render(<InstallHint />)
      await user.click(screen.getByRole('button', { name: 'Dismiss' }))
      expect(screen.queryByRole('note', HINT)).not.toBeInTheDocument()
    } finally {
      getItem.mockRestore()
      setItem.mockRestore()
    }
  })
})

describe('InstallHint in the shell', () => {
  function renderAt(path: string) {
    mockApi({
      'GET /api/auth/me': { body: TEST_USER },
      'GET /api/health': { body: { status: 'ok', version: '0.1.0', env: 'test' } },
      'GET /api/list': { body: [] },
      'GET /api/review/summary': { body: { pending: 0 } },
    })
    const router = createMemoryRouter(routes, { initialEntries: [path] })
    render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
  }

  it('sits above Watch Now on an iPad in Safari', async () => {
    stubDevice({ ua: UA.ipadSafari, touchPoints: 5 })
    renderAt('/')

    expect(await screen.findByRole('note', HINT)).toBeInTheDocument()
  })

  it('is not on any other page', async () => {
    stubDevice({ ua: UA.ipadSafari, touchPoints: 5 })
    renderAt('/schedule')

    await screen.findByRole('button', { name: 'Account' })
    expect(screen.queryByRole('note', HINT)).not.toBeInTheDocument()
  })
})
