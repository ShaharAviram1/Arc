import { useState, type FormEvent } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { AuthShell, FormError, fieldClass, labelClass, submitClass } from '@/components/AuthShell'
import { AuthPending } from '@/components/RequireAuth'
import {
  authErrorMessage,
  browserTimezone,
  isEmailTaken,
  isStatus,
  MIN_PASSWORD_LENGTH,
  useAcceptInvite,
  useInvite,
} from '@/lib/auth'

function formatExpiry(value: string): string | null {
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return null
  return at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function Invite() {
  const { token = '' } = useParams<{ token: string }>()
  const invite = useInvite(token)
  const accept = useAcceptInvite(token)

  const [typedEmail, setTypedEmail] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [clientError, setClientError] = useState<string | null>(null)

  // Accepting logs the new account in, so there is somewhere to go.
  if (accept.isSuccess) return <Navigate to="/" replace />

  if (invite.isPending) return <AuthPending />

  if (invite.isError || !invite.data) {
    // A 404 really does mean "used up or never existed" (the server answers
    // 404 for every bad token on purpose). Anything else — a 500, an offline
    // browser — is about Arc, not about the link, and saying "invite not
    // valid" makes people throw away a link that still works.
    const spent = isStatus(invite.error, 404) || !invite.isError
    return (
      <AuthShell
        title={spent ? 'Invite not valid' : 'Could not check that invite'}
        subtitle={
          spent
            ? 'This invite link is invalid or has already been used.'
            : authErrorMessage(invite.error)
        }
      >
        <Link className="text-sm text-[var(--arc-accent)] hover:underline" to="/login">
          Go to sign in
        </Link>
      </AuthShell>
    )
  }

  const fixedEmail = invite.data.email
  const emailValue = typedEmail ?? fixedEmail ?? ''
  const expiry = formatExpiry(invite.data.expires_at)

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const email = emailValue.trim()
    if (fixedEmail === null && email === '') {
      setClientError('Enter your email address.')
      return
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setClientError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`)
      return
    }
    if (password !== confirm) {
      setClientError('Passwords do not match.')
      return
    }

    setClientError(null)
    accept.mutate({
      ...(fixedEmail === null ? { email } : {}),
      password,
      timezone: browserTimezone(),
    })
  }

  const conflict = isEmailTaken(accept.error)
  const serverError = accept.isError ? authErrorMessage(accept.error) : null

  return (
    <AuthShell
      title="Set up your account"
      subtitle={
        expiry
          ? `Choose a password to finish creating your Arc account. This invite expires ${expiry}.`
          : 'Choose a password to finish creating your Arc account.'
      }
    >
      <form onSubmit={handleSubmit} noValidate>
        <div>
          <label className={labelClass} htmlFor="invite-email">
            Email
          </label>
          <input
            id="invite-email"
            name="email"
            type="email"
            autoComplete="username"
            required
            readOnly={fixedEmail !== null}
            value={emailValue}
            onChange={(event) => {
              setTypedEmail(event.target.value)
            }}
            className={fieldClass}
          />
          {fixedEmail !== null ? (
            <p className="mt-1 text-xs text-[var(--arc-text-muted)]">
              This invite is tied to this address.
            </p>
          ) : null}
        </div>

        <div className="mt-4">
          <label className={labelClass} htmlFor="invite-password">
            Password
          </label>
          <input
            id="invite-password"
            name="new-password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(event) => {
              setPassword(event.target.value)
              // The complaint was about what was typed; typing answers it.
              setClientError(null)
            }}
            className={fieldClass}
            aria-describedby="invite-password-hint"
          />
          <p id="invite-password-hint" className="mt-1 text-xs text-[var(--arc-text-muted)]">
            At least {MIN_PASSWORD_LENGTH} characters.
          </p>
        </div>

        <div className="mt-4">
          <label className={labelClass} htmlFor="invite-confirm">
            Confirm password
          </label>
          <input
            id="invite-confirm"
            name="confirm-password"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(event) => {
              setConfirm(event.target.value)
              setClientError(null)
            }}
            className={fieldClass}
          />
        </div>

        {clientError !== null ? <FormError>{clientError}</FormError> : null}
        {clientError === null && serverError !== null ? (
          <FormError>
            {serverError}
            {conflict ? (
              <>
                {' '}
                <Link className="text-[var(--arc-accent)] underline" to="/login">
                  Sign in instead.
                </Link>
              </>
            ) : null}
          </FormError>
        ) : null}

        <button type="submit" className={submitClass} disabled={accept.isPending}>
          {accept.isPending ? 'Creating account…' : 'Create account'}
        </button>
      </form>
    </AuthShell>
  )
}
