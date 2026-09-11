/**
 * Users and invites (spec §4.10 FR-D1, roadmap M14).
 *
 * Two tables and a form. The rules that matter are the server's — an admin may
 * not deactivate or demote themselves, and the last active admin may not be
 * removed by anyone (`arc/api/users.py`) — and this half of it exists so that
 * the first of those never reaches the server as a 409 in the first place: the
 * viewer's own row has no buttons, and says why.
 *
 * The last-admin rule is *not* mirrored here. It is a race the client cannot
 * see (two admins, each demoting the other) and a check that would go stale the
 * moment somebody else pressed something; the 409 is the answer, and it is
 * rendered verbatim because the server's sentence is better than any invented
 * here.
 *
 * The invite link is shown once and never cached. `useCreateInvite` returns it,
 * this component holds it in state, and switching tabs loses it — which is the
 * intended behaviour, not an oversight: the database keeps only the hash, so a
 * link that survived in a query cache would be a token that came back on a
 * page revisit long after the admin thought it was gone.
 */

import { useState } from 'react'
import { ErrorState } from '@/components/ErrorState'
import {
  ConfirmButton,
  InlineError,
  Notice,
  panelClass,
  Pill,
  SectionHeading,
  TableScroll,
  tdClass,
  thClass,
} from '@/components/admin/ui'
import { inputClass, primaryButtonClass, subtleButtonClass } from '@/components/admin/styles'
import { LABEL_CLASS } from '@/components/ui'
import {
  DEFAULT_EXPIRY_HOURS,
  MAX_EXPIRY_HOURS,
  adminErrorMessage,
  isoDate,
  useAdminUsers,
  useCreateInvite,
  useDeleteInvite,
  useInvites,
  useUpdateUser,
  type AdminAccount,
  type InviteCreated,
  type InviteRow,
  type InviteStatus,
} from '@/lib/admin'
import { useMe } from '@/lib/auth'

const LINK_WARNING =
  'Copy it now — it is shown once. Arc stores only a hash of the token, so if this is lost the ' +
  'invite has to be issued again.'

const INVITE_EXPLANATION =
  'An invite is a one-time link. Bind it to an address to fix who may use it, or leave the address ' +
  'blank for a link anyone with it can redeem.'

const INVITE_STATUS_TONE: Record<InviteStatus, 'ok' | 'muted' | 'warn'> = {
  pending: 'ok',
  used: 'muted',
  expired: 'warn',
}

/** One account. The viewer's own row is deliberately inert (see the header). */
function UserRow({
  user,
  isSelf,
  onPatch,
  pending,
  error,
}: {
  user: AdminAccount
  isSelf: boolean
  onPatch: (patch: { is_active?: boolean; role?: 'admin' | 'user' }) => void
  pending: boolean
  error: string | null
}) {
  const promoting = user.role !== 'admin'

  return (
    <tr className="border-t border-[var(--arc-border)]">
      <td className={tdClass}>
        <span className="font-medium">{user.email}</span>
        {isSelf ? (
          <span className="ml-2 text-[13px] text-[var(--arc-text-muted)]">(you)</span>
        ) : null}
      </td>
      <td className={tdClass}>
        <Pill tone={user.role === 'admin' ? 'busy' : 'muted'}>{user.role}</Pill>
      </td>
      <td className={tdClass}>
        <Pill tone={user.is_active ? 'ok' : 'bad'}>{user.is_active ? 'active' : 'disabled'}</Pill>
      </td>
      <td className={`${tdClass} whitespace-nowrap text-[var(--arc-text-muted)]`}>
        {isoDate(user.created_at)}
      </td>
      <td className={tdClass}>
        <div className="flex flex-wrap items-center gap-2">
          {isSelf ? (
            <span className="text-[13px] text-[var(--arc-text-muted)]">
              Your own account; another admin can change it
            </span>
          ) : (
            <>
              <button
                type="button"
                className={subtleButtonClass}
                disabled={pending}
                onClick={() => {
                  onPatch({ role: promoting ? 'admin' : 'user' })
                }}
              >
                {promoting ? 'Make admin' : 'Make user'}
              </button>
              {user.is_active ? (
                <ConfirmButton
                  label="Deactivate"
                  question={`Deactivate ${user.email}?`}
                  confirmLabel="Yes, deactivate"
                  pending={pending}
                  onConfirm={() => {
                    onPatch({ is_active: false })
                  }}
                />
              ) : (
                <button
                  type="button"
                  className={subtleButtonClass}
                  disabled={pending}
                  onClick={() => {
                    onPatch({ is_active: true })
                  }}
                >
                  Reactivate
                </button>
              )}
            </>
          )}
        </div>
        {error === null ? null : <InlineError className="mt-1" message={error} />}
      </td>
    </tr>
  )
}

function AccountsPanel() {
  const { data: me } = useMe()
  const users = useAdminUsers()
  const update = useUpdateUser()

  if (users.isPending) {
    return (
      <p role="status" className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
        Loading accounts…
      </p>
    )
  }

  if (users.isError) {
    return (
      <ErrorState
        className="mt-3"
        message={adminErrorMessage(users.error)}
        pending={users.isFetching}
        onRetry={() => {
          void users.refetch()
        }}
      />
    )
  }

  return (
    <TableScroll>
      <table className="min-w-full border-collapse">
        <caption className="sr-only">Accounts</caption>
        <thead>
          <tr>
            <th className={thClass}>Email</th>
            <th className={thClass}>Role</th>
            <th className={thClass}>Status</th>
            <th className={thClass}>Created</th>
            <th className={thClass}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.data.map((user) => (
            <UserRow
              key={user.id}
              user={user}
              isSelf={me?.id === user.id}
              pending={update.isPending && update.variables?.id === user.id}
              error={
                update.isError && update.variables?.id === user.id
                  ? adminErrorMessage(update.error)
                  : null
              }
              onPatch={(patch) => {
                update.mutate({ id: user.id, patch })
              }}
            />
          ))}
        </tbody>
      </table>
    </TableScroll>
  )
}

/** The link, once, with the warning that says why it will not be here later. */
function CreatedInvite({ invite, onDismiss }: { invite: InviteCreated; onDismiss: () => void }) {
  const [copied, setCopied] = useState<boolean | null>(null)

  async function copy() {
    try {
      await navigator.clipboard.writeText(invite.url)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className={`mt-4 ${panelClass}`}>
      <Notice>Invite created{invite.email === null ? '' : ` for ${invite.email}`}.</Notice>
      <p className="mt-2 text-[14px] font-medium text-[var(--arc-text-muted)]">{LINK_WARNING}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto rounded-[12px] border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface-input)] px-3.5 py-3 font-mono text-[13px] whitespace-nowrap text-[var(--arc-text)]">
          {invite.url}
        </code>
        <button
          type="button"
          className={subtleButtonClass}
          onClick={() => {
            void copy()
          }}
        >
          Copy link
        </button>
        <button type="button" className={subtleButtonClass} onClick={onDismiss}>
          Dismiss
        </button>
      </div>
      {copied === null ? null : copied ? (
        <Notice className="mt-2">Copied to the clipboard.</Notice>
      ) : (
        <InlineError
          className="mt-2"
          message="Could not reach the clipboard. Select the link above and copy it by hand."
        />
      )}
    </div>
  )
}

function InvitesPanel() {
  const invites = useInvites()
  const create = useCreateInvite()
  const remove = useDeleteInvite()

  const [email, setEmail] = useState('')
  const [hours, setHours] = useState(String(DEFAULT_EXPIRY_HOURS))
  const [created, setCreated] = useState<InviteCreated | null>(null)

  function submit() {
    const trimmed = email.trim()
    create.mutate(
      { email: trimmed === '' ? null : trimmed, expires_in_hours: Number(hours) },
      {
        onSuccess: (invite) => {
          setCreated(invite)
          setEmail('')
        },
      },
    )
  }

  return (
    <section className="mt-10">
      <SectionHeading>Invites</SectionHeading>
      <p className="mt-2 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        {INVITE_EXPLANATION}
      </p>

      <form
        className="mt-4 flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor="invite-email" className={LABEL_CLASS}>
            Email (optional)
          </label>
          <input
            id="invite-email"
            type="email"
            value={email}
            placeholder="leah@example.com"
            disabled={create.isPending}
            onChange={(event) => {
              setEmail(event.target.value)
            }}
            className={`w-64 ${inputClass}`}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="invite-hours" className={LABEL_CLASS}>
            Expires in (hours)
          </label>
          <input
            id="invite-hours"
            type="number"
            min={1}
            max={MAX_EXPIRY_HOURS}
            value={hours}
            disabled={create.isPending}
            onChange={(event) => {
              setHours(event.target.value)
            }}
            className={`w-32 ${inputClass}`}
          />
        </div>
        <button type="submit" className={primaryButtonClass} disabled={create.isPending}>
          {create.isPending ? 'Creating…' : 'Create invite'}
        </button>
      </form>

      {create.isError ? (
        <InlineError className="mt-2" message={adminErrorMessage(create.error)} />
      ) : null}

      {created === null ? null : (
        <CreatedInvite
          invite={created}
          onDismiss={() => {
            setCreated(null)
          }}
        />
      )}

      {invites.isPending ? (
        <p role="status" className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
          Loading invites…
        </p>
      ) : invites.isError ? (
        <ErrorState
          className="mt-4"
          message={adminErrorMessage(invites.error)}
          pending={invites.isFetching}
          onRetry={() => {
            void invites.refetch()
          }}
        />
      ) : invites.data.length === 0 ? (
        <p className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
          No invites have been issued.
        </p>
      ) : (
        <div className="mt-4">
          <TableScroll>
            <table className="min-w-full border-collapse">
              <caption className="sr-only">Invites</caption>
              <thead>
                <tr>
                  <th className={thClass}>Email</th>
                  <th className={thClass}>Status</th>
                  <th className={thClass}>Created</th>
                  <th className={thClass}>Expires</th>
                  <th className={thClass}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {invites.data.map((invite: InviteRow) => (
                  <tr key={invite.id} className="border-t border-[var(--arc-border)]">
                    <td className={tdClass}>{invite.email ?? 'anyone with the link'}</td>
                    <td className={tdClass}>
                      <Pill tone={INVITE_STATUS_TONE[invite.status]}>{invite.status}</Pill>
                    </td>
                    <td className={`${tdClass} whitespace-nowrap text-[var(--arc-text-muted)]`}>
                      {isoDate(invite.created_at)}
                    </td>
                    <td className={`${tdClass} whitespace-nowrap text-[var(--arc-text-muted)]`}>
                      {isoDate(invite.expires_at)}
                    </td>
                    <td className={tdClass}>
                      {invite.status === 'pending' ? (
                        <button
                          type="button"
                          className={subtleButtonClass}
                          disabled={remove.isPending && remove.variables === invite.id}
                          onClick={() => {
                            remove.mutate(invite.id)
                          }}
                        >
                          Revoke
                        </button>
                      ) : (
                        <span className="text-[13px] text-[var(--arc-text-muted)]">—</span>
                      )}
                      {remove.isError && remove.variables === invite.id ? (
                        <InlineError className="mt-1" message={adminErrorMessage(remove.error)} />
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </div>
      )}
    </section>
  )
}

export function UsersTab() {
  return (
    <div>
      <SectionHeading>Accounts</SectionHeading>
      <p className="mt-2 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        A deactivated account is signed out on its next request and cannot sign in again. Arc keeps
        at least one active admin: the server refuses the change that would leave none.
      </p>
      <div className="mt-4">
        <AccountsPanel />
      </div>
      <InvitesPanel />
    </div>
  )
}
