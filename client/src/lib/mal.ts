/**
 * MyAnimeList link and sync-log data layer (spec §4.7 FR-M1–FR-M7, §5,
 * roadmap M9).
 *
 * Two things live here. The **link** — an OAuth grant the server holds on the
 * viewer's behalf — which the client only ever starts, inspects, or tears
 * down; and the **write log**, the record of every change Arc pushed to MAL
 * with the value it replaced (FR-M5). The log is the reason FR-M7 is
 * checkable from the outside: if Arc wrote it, it is in this list, with the
 * user event that caused it.
 *
 * Starting a link is a full-page navigation, not a fetch: the server answers
 * with MAL's authorize URL and the browser has to *go there* so the person can
 * sign in on MAL's own origin. That is the one place in the client where a
 * mutation ends by leaving the app.
 *
 * Everything an import, a push or a revert does lands in two caches — the MAL
 * views here and the show pages, whose `list_entry.mal_sync` badge is derived
 * from the same writes — so every mutation invalidates both.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { ApiError, apiFetch } from '@/lib/api'
import { ANIME_QUERY_KEY, isListStatus, LIST_STATUS_LABELS, type AnimeSummary } from '@/lib/anime'
import { errorDetail } from '@/lib/auth'

/** `GET /api/mal/status` — the state of this viewer's MAL link. */
export interface MalStatus {
  linked: boolean
  /** The MAL account Arc is linked to; null when there is no link. */
  mal_username: string | null
  /** When the current access token expires; null when there is no link. */
  expires_at: string | null
  /** Last successful import of the MAL list (FR-M2, FR-M3). */
  last_import_at: string | null
  /** The refresh token no longer works: the person has to authorise again. */
  needs_relink: boolean
  /**
   * Fields queued but not yet accepted by MAL (FR-M6). One per queued row, so
   * a single show whose status, score and progress all moved counts three.
   */
  pending_writes: number
  /** Writes that exhausted their retries and need attention (FR-M6). */
  failed_writes: number
  /** False when the server has no MAL client id, so linking cannot be offered. */
  configured: boolean
}

/** The three fields Arc is allowed to write to MAL (FR-M4). */
export type MalWriteField = 'status' | 'score' | 'progress'

/**
 * What caused a write. Every one of these is user-originated (FR-M7):
 * `watch` is an episode completed, `manual` an explicit edit in Arc, `revert`
 * an entry undone from this log, `conflict` the losing side of a two-sided
 * change being written back (FR-M3).
 */
export type MalWriteCause = 'watch' | 'manual' | 'revert' | 'conflict'

/**
 * Where a write got to. `pending` is written before the request goes out, so a
 * crash mid-write leaves evidence rather than silence; it becomes `ok` or
 * `failed` when MAL answers. `skipped` means nothing was sent at all — the
 * FR-M4 rule declining to lower progress, a change overtaken by a later one, a
 * value MAL already had.
 */
export type MalWriteStatus = 'pending' | 'ok' | 'failed' | 'skipped'

/** One row of `GET /api/mal/log` — a write, and the value it replaced (FR-M5). */
export interface MalWrite {
  id: number
  /** Null when the show has since left the catalogue; the row still stands. */
  anime: AnimeSummary | null
  field: MalWriteField
  /** The value before the write; `formatMalValue` turns it into words. */
  old_value: unknown
  new_value: unknown
  cause: MalWriteCause
  status: MalWriteStatus
  /**
   * MAL's own complaint, for a `failed` row — and for a `skipped` one, always
   * a sentence saying why nothing was sent ("automatic progress never lowers
   * MAL"), which is an explanation rather than a fault.
   */
  error: string | null
  created_at: string
  /** False once a row has been reverted, or when there is nothing to go back to. */
  revertible: boolean
}

/** Per-show sync state, carried on `AnimeDetail.list_entry` (FR-M6). */
export interface MalSync {
  state: 'synced' | 'pending' | 'failed' | 'unlinked'
  error: string | null
  last_write_at: string | null
}

/** Which rows the log page is asking for. `status` unset means every row. */
export interface MalLogFilters {
  limit?: number
  status?: MalWriteStatus
}

export const MAL_QUERY_KEY = 'mal'

export const malStatusQueryKey = [MAL_QUERY_KEY, 'status'] as const

export function malLogQueryKey(
  filters: MalLogFilters = {},
): readonly [string, string, string, number | 'default'] {
  return [MAL_QUERY_KEY, 'log', filters.status ?? 'all', filters.limit ?? 'default']
}

/** How many rows the log asks for when the caller does not say. */
export const MAL_LOG_LIMIT = 50

/** What a null / absent value reads as, in a table cell or a value pair. */
export const EMPTY_VALUE = '—'

export const MAL_FIELD_LABELS: Record<MalWriteField, string> = {
  status: 'Status',
  score: 'Score',
  progress: 'Progress',
}

export const MAL_CAUSE_LABELS: Record<MalWriteCause, string> = {
  watch: 'Watched',
  manual: 'Manual change',
  revert: 'Revert',
  conflict: 'Conflict',
}

export const MAL_WRITE_STATUS_LABELS: Record<MalWriteStatus, string> = {
  pending: 'Pending',
  ok: 'Synced',
  failed: 'Failed',
  skipped: 'Skipped',
}

const OK_BADGE = 'text-[var(--arc-ok)] border-[var(--arc-ok)]/40 bg-[var(--arc-ok)]/10'
const BUSY_BADGE =
  'text-[var(--arc-accent)] border-[var(--arc-accent)]/40 bg-[var(--arc-accent)]/10'
const BAD_BADGE = 'text-[var(--arc-error)] border-[var(--arc-error)]/40 bg-[var(--arc-error)]/10'
const MUTED_BADGE =
  'text-[var(--arc-text-muted)] border-[var(--arc-border)] bg-[var(--arc-surface-raised)]'

export const MAL_WRITE_STATUS_CLASSES: Record<MalWriteStatus, string> = {
  ok: OK_BADGE,
  pending: BUSY_BADGE,
  failed: BAD_BADGE,
  // Skipped is not a failure — the FR-M4 rule working — so it reads as quiet.
  skipped: MUTED_BADGE,
}

/**
 * A value as a person reads it. A status comes back as the wire enum
 * (`on_hold`), which is not what the rest of the client shows anywhere else;
 * a score or a progress count is already a number. Everything absent — a
 * first-ever write has no previous value — is the same em dash the score
 * control uses for "unset".
 */
export function formatMalValue(field: MalWriteField, value: unknown): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  if (typeof value === 'number') return String(value)
  if (typeof value === 'string') {
    if (value === '') return EMPTY_VALUE
    if (field === 'status' && isListStatus(value)) return LIST_STATUS_LABELS[value]
    return value
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  // Nothing the server sends should land here; showing the JSON beats a blank
  // cell if it ever does.
  return JSON.stringify(value)
}

/** A timestamp as a date and a time; the log is read in minutes, not days. */
export function formatMalDate(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return EMPTY_VALUE
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return EMPTY_VALUE
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(at)
}

/**
 * The `error` codes MAL (or Arc's callback) can send back on the redirect.
 * Anything not listed is shown with its code, which is at least something to
 * quote in a bug report.
 */
const MAL_LINK_ERRORS: Record<string, string> = {
  access_denied: 'You declined the request on MyAnimeList, so nothing was linked.',
  invalid_state: 'That link attempt expired or was tampered with. Try connecting again.',
  state_mismatch: 'That link attempt expired or was tampered with. Try connecting again.',
  missing_code: 'MyAnimeList did not send an authorisation code back. Try connecting again.',
  token_exchange_failed: 'MyAnimeList refused to issue a token. Try connecting again.',
  not_configured: 'This server has no MyAnimeList client id, so linking is off.',
  server_error: 'MyAnimeList had a problem finishing the link. Try again in a few minutes.',
}

export function malLinkErrorMessage(code: string): string {
  return MAL_LINK_ERRORS[code] ?? `MyAnimeList could not be linked (${code}).`
}

/** What to say when a MAL call fails; the log page shows this verbatim. */
export function malErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not reach the server. Try again.'
  switch (error.status) {
    case 401:
      return 'Your session expired. Sign in again.'
    case 404:
      return 'That entry is gone.'
    case 409:
      return errorDetail(error) ?? 'That is no longer possible — reload the page.'
    case 503:
      return 'This server has no MyAnimeList client id, so linking is off.'
    default:
      return 'Something went wrong. Try again.'
  }
}

/** The link state for this viewer. Cheap, and every action here changes it. */
export function useMalStatus(): UseQueryResult<MalStatus, Error> {
  return useQuery<MalStatus, Error>({
    queryKey: malStatusQueryKey,
    queryFn: () => apiFetch<MalStatus>('/api/mal/status'),
    retry: false,
  })
}

export function useMalLog(filters: MalLogFilters = {}): UseQueryResult<MalWrite[], Error> {
  const limit = filters.limit ?? MAL_LOG_LIMIT
  const status = filters.status

  return useQuery<MalWrite[], Error>({
    queryKey: malLogQueryKey({ limit, status }),
    queryFn: () => {
      const params = new URLSearchParams({ limit: String(limit) })
      if (status !== undefined) params.set('status', status)
      return apiFetch<MalWrite[]>(`/api/mal/log?${params.toString()}`)
    },
    retry: false,
  })
}

/** `POST /api/mal/link`: where MAL wants the person to sign in. */
interface MalLinkStart {
  authorize_url: string
}

/**
 * Start the OAuth flow (FR-M1). The server mints the PKCE challenge and the
 * state, so all the client does with the answer is *go there* — a fetch of the
 * authorize URL would land in a login page the app cannot render and would
 * lose the redirect back to `/mal`.
 *
 * There is deliberately no cache work on success: the page is about to be
 * replaced by MAL's.
 */
export function useStartMalLink(): UseMutationResult<MalLinkStart, Error, void> {
  return useMutation<MalLinkStart, Error, void>({
    mutationFn: () => apiFetch<MalLinkStart>('/api/mal/link', { method: 'POST' }),
    onSuccess: ({ authorize_url }) => {
      window.location.assign(authorize_url)
    },
  })
}

/**
 * Every MAL action changes both the link state and what a show page says about
 * its own sync (`list_entry.mal_sync`), and all of it is server-derived — a
 * queued push, an import that rewrote list entries — so there is nothing
 * honest to patch a cache to. Both trees are invalidated and re-asked.
 */
function useMalMutation<TResult>(
  request: () => Promise<TResult>,
): UseMutationResult<TResult, Error, void> {
  const client = useQueryClient()

  return useMutation<TResult, Error, void>({
    mutationFn: request,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [MAL_QUERY_KEY] })
      void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY] })
    },
  })
}

/** Drop the link and the stored tokens (FR-M1). Arc stops writing at once. */
export function useUnlinkMal(): UseMutationResult<null, Error, void> {
  return useMalMutation<null>(() => apiFetch<null>('/api/mal/link', { method: 'DELETE' }))
}

/** Re-import the MAL list as the baseline (FR-M2). 202: a job does the work. */
export function useMalImport(): UseMutationResult<null, Error, void> {
  return useMalMutation<null>(() => apiFetch<null>('/api/mal/import', { method: 'POST' }))
}

/** Retry the queued writes now instead of waiting for the backoff (FR-M6). */
export function useMalPush(): UseMutationResult<null, Error, void> {
  return useMalMutation<null>(() => apiFetch<null>('/api/mal/push', { method: 'POST' }))
}

/**
 * Put a logged write's previous value back (FR-M5). This is itself a
 * user-originated write, so it is queued and logged like any other — the log
 * grows a `revert` row rather than the original one disappearing.
 */
export function useRevertMalWrite(): UseMutationResult<null, Error, number> {
  const client = useQueryClient()

  return useMutation<null, Error, number>({
    mutationFn: (id) => apiFetch<null>(`/api/mal/log/${String(id)}/revert`, { method: 'POST' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [MAL_QUERY_KEY] })
      void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY] })
    },
  })
}
