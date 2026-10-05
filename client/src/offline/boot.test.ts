import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { flushPendingLogout, isUser, loadMe } from '@/lib/auth'
import { createQueryClient } from '@/lib/queryClient'
import {
  forgetSession,
  HOME_PAYLOAD,
  isLogoutPending,
  markLogoutPending,
  recallPayload,
  recallPosition,
  recallUser,
  rememberPayload,
  rememberPosition,
  rememberUser,
  withRemembered,
} from '@/offline/cache'
import { TEST_ADMIN, TEST_USER } from '@/test/apiMock'

const offline = () => Promise.reject(new TypeError('Load failed'))

describe('launching with no network (FR-S9)', () => {
  it('remembers the user a successful /me answered with', async () => {
    expect(await loadMe(() => Promise.resolve(TEST_USER))).toEqual(TEST_USER)
    // Written in the background: the answer does not wait for IndexedDB.
    await vi.waitFor(async () => {
      expect(await recallUser()).toEqual(TEST_USER)
    })
  })

  it('answers a network failure with the remembered user: it is not a sign-out', async () => {
    await rememberUser(TEST_USER)
    expect(await loadMe(offline)).toEqual(TEST_USER)
  })

  it('does the same when only the proxy answered, for a server that is down', async () => {
    await rememberUser(TEST_USER)
    expect(await loadMe(() => Promise.reject(new ApiError(502, null)))).toEqual(TEST_USER)
  })

  it('signs out on a real 401, and forgets the user for the next offline launch', async () => {
    await rememberUser(TEST_USER)
    expect(await loadMe(() => Promise.reject(new ApiError(401, null)))).toBeNull()
    await vi.waitFor(async () => {
      expect(await recallUser()).toBeNull()
    })
    await expect(loadMe(offline)).rejects.toThrow('Load failed')
  })

  it('still fails with no remembered user: this device never signed in', async () => {
    await expect(loadMe(offline)).rejects.toThrow('Load failed')
  })

  it('does not paper over a server bug', async () => {
    await rememberUser(TEST_USER)
    await expect(loadMe(() => Promise.reject(new ApiError(500, null)))).rejects.toBeInstanceOf(
      ApiError,
    )
  })
})

describe('remembered pages', () => {
  it('serve the last good copy when the server cannot be reached', async () => {
    const page = { greeting: 'hi' }
    expect(await withRemembered(HOME_PAYLOAD, () => Promise.resolve(page))).toEqual(page)
    expect(await withRemembered(HOME_PAYLOAD, offline)).toEqual(page)
  })

  it('never stand in for an answer the server actually gave', async () => {
    await rememberPayload(HOME_PAYLOAD, { greeting: 'hi' })
    await expect(
      withRemembered(HOME_PAYLOAD, () => Promise.reject(new ApiError(403, null))),
    ).rejects.toBeInstanceOf(ApiError)
  })

  it('are dropped when a different account signs in', async () => {
    await rememberUser(TEST_USER)
    await rememberPayload(HOME_PAYLOAD, { greeting: 'hi' })
    await rememberUser(TEST_ADMIN)
    expect(await recallPayload(HOME_PAYLOAD)).toBeUndefined()
  })
})

describe('sign-out', () => {
  it('forgets the user and the pages, but keeps positions on the device', async () => {
    await rememberUser(TEST_USER)
    await rememberPayload(HOME_PAYLOAD, { greeting: 'hi' })
    await rememberPosition(TEST_USER.id, 9001, 312, 1436.8)

    await forgetSession()

    expect(await recallUser()).toBeNull()
    expect(await recallPayload(HOME_PAYLOAD)).toBeUndefined()
    expect(await recallPosition(TEST_USER.id, 9001)).toMatchObject({ position_s: 312 })
    // Positions are per account: another one has none.
    expect(await recallPosition(TEST_ADMIN.id, 9001)).toBeNull()
  })
})

describe('a real session loss forgets the offline copies (S3)', () => {
  it('on a 401 from /me: the user and the pages go', async () => {
    await rememberUser(TEST_USER)
    await rememberPayload(HOME_PAYLOAD, { greeting: 'hi' })

    expect(await loadMe(() => Promise.reject(new ApiError(401, null)))).toBeNull()

    await vi.waitFor(async () => {
      expect(await recallUser()).toBeNull()
      expect(await recallPayload(HOME_PAYLOAD)).toBeUndefined()
    })
  })

  it('on a 401 from any other query, so the next offline launch is signed out', async () => {
    await rememberUser(TEST_USER)
    const client = createQueryClient()

    await client
      .fetchQuery({
        queryKey: ['probe'],
        queryFn: () => Promise.reject(new ApiError(401, null)),
        retry: false,
      })
      .catch(() => undefined)

    await vi.waitFor(async () => {
      expect(await recallUser()).toBeNull()
    })
    await expect(loadMe(() => Promise.reject(new TypeError('Load failed')))).rejects.toThrow()
  })
})

describe("one account's pages never reach another (S4)", () => {
  it('drops pages nobody owned when an account signs in', async () => {
    await rememberPayload(HOME_PAYLOAD, { greeting: 'from before' }, null)
    await rememberUser(TEST_USER)
    expect(await recallPayload(HOME_PAYLOAD)).toBeUndefined()
  })

  it('stamps a page with the account that asked, even if it lands after a switch', async () => {
    await rememberUser(TEST_USER)
    let answer: (value: { greeting: string }) => void = () => undefined
    const pending = withRemembered(
      HOME_PAYLOAD,
      () =>
        new Promise<{ greeting: string }>((resolve) => {
          answer = resolve
        }),
    )
    // Let the owner be read before the switch.
    await new Promise((resolve) => setTimeout(resolve, 0))
    await forgetSession()
    await rememberUser(TEST_ADMIN)
    answer({ greeting: 'for the viewer' })
    await pending
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(await recallPayload(HOME_PAYLOAD)).toBeUndefined()
  })
})

describe('a sign-out made with no network (S8)', () => {
  it('keeps the app signed out until the server has been told', async () => {
    await rememberUser(TEST_USER)
    await markLogoutPending()
    const me = vi.fn(() => Promise.resolve(TEST_USER))

    const result = await loadMe(me, () =>
      flushPendingLogout(() => Promise.reject(new TypeError('Load failed'))),
    )

    expect(result).toBeNull()
    expect(me).not.toHaveBeenCalled()
    expect(await isLogoutPending()).toBe(true)
  })

  it('tells the server first, then trusts /me', async () => {
    await markLogoutPending()
    const calls: string[] = []

    const result = await loadMe(
      () => {
        calls.push('me')
        return Promise.reject(new ApiError(401, null))
      },
      () =>
        flushPendingLogout(() => {
          calls.push('logout')
          return Promise.resolve(null)
        }),
    )

    expect(calls).toEqual(['logout', 'me'])
    expect(result).toBeNull()
    expect(await isLogoutPending()).toBe(false)
  })

  it('counts a 401 to the logout as done: the session was gone already', async () => {
    await markLogoutPending()
    expect(await flushPendingLogout(() => Promise.reject(new ApiError(401, null)))).toBe(true)
    expect(await isLogoutPending()).toBe(false)
  })

  it('has nothing to send when nothing is pending', async () => {
    const post = vi.fn(() => Promise.resolve(null))
    expect(await flushPendingLogout(post)).toBe(true)
    expect(post).not.toHaveBeenCalled()
  })
})

describe('the remembered user is checked before it is kept', () => {
  it('refuses an answer that is not a user', async () => {
    await expect(
      loadMe(() => Promise.resolve({ unexpected: true } as unknown as typeof TEST_USER)),
    ).rejects.toThrow(/something else/)
    expect(isUser(TEST_USER)).toBe(true)
    expect(await recallUser()).toBeNull()
  })
})
