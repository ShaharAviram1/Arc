import { useState, type FormEvent } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { AuthShell, FormError } from '@/components/AuthShell'
import { buttonClass, inputClass, LABEL_CLASS } from '@/components/ui'
import { authErrorMessage, useLogin, useMe } from '@/lib/auth'

/** A string field of an untyped router-state object, or '' when it is absent. */
function stringField(source: object, key: string): string {
  if (!(key in source)) return ''
  const value: unknown = (source as Record<string, unknown>)[key]
  return typeof value === 'string' ? value : ''
}

/**
 * `RequireAuth` stashes the whole blocked `location` in `location.state.from`.
 * Router state is untyped, so it is narrowed by hand, reassembled, and then
 * resolved against our own origin: anything that lands somewhere else
 * (`//evil.com`, `/\evil.com`, `http://x`, `javascript:…`) is not a path on
 * this site and must never become a redirect. A same-origin target keeps its
 * query and hash so a deep link survives the round trip through login.
 */
function redirectTarget(state: unknown): string {
  if (typeof state !== 'object' || state === null || !('from' in state)) return '/'
  const from: unknown = state.from
  if (typeof from !== 'object' || from === null || !('pathname' in from)) return '/'
  const pathname = stringField(from, 'pathname')
  if (pathname === '') return '/'

  const origin = window.location.origin
  let url: URL
  try {
    url = new URL(pathname + stringField(from, 'search') + stringField(from, 'hash'), origin)
  } catch {
    return '/'
  }

  if (url.origin !== origin) return '/'
  if (url.pathname === '/login') return '/'
  return url.pathname + url.search + url.hash
}

export function Login() {
  const location = useLocation()
  const state: unknown = location.state
  const { data: me } = useMe()
  const login = useLogin()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const target = redirectTarget(state)

  // Covers both "already had a session" and "just logged in": the login
  // mutation writes the user into the `me` cache, which re-renders us here.
  if (me) return <Navigate to={target} replace />

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    login.mutate({ email, password })
  }

  return (
    <AuthShell
      title="Sign in"
      subtitle="Arc is invite-only — there is no sign-up. Ask an admin for an invite link."
    >
      <form onSubmit={handleSubmit} noValidate>
        <div>
          <label className={LABEL_CLASS} htmlFor="login-email">
            Email
          </label>
          <input
            id="login-email"
            name="email"
            type="email"
            autoComplete="username"
            autoFocus
            required
            value={email}
            onChange={(event) => {
              setEmail(event.target.value)
            }}
            className={inputClass('mt-2 w-full')}
          />
        </div>

        <div className="mt-4">
          <label className={LABEL_CLASS} htmlFor="login-password">
            Password
          </label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => {
              setPassword(event.target.value)
            }}
            className={inputClass('mt-2 w-full')}
          />
        </div>

        {login.isError ? <FormError>{authErrorMessage(login.error)}</FormError> : null}

        <button
          type="submit"
          className={buttonClass('primary', 'mt-7 w-full')}
          disabled={login.isPending}
        >
          {login.isPending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </AuthShell>
  )
}
