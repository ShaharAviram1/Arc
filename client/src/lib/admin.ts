/**
 * Admin data layer (spec §4.10 FR-D1–FR-D4, §4.9 FR-T4, roadmap M14).
 *
 * Everything the admin page needs, in one module because the page is one page:
 * accounts and invites, the rules that drive acquisition and retention, the
 * job queue, disk, and what qBittorrent is doing. The tabs are separate
 * components; the wire is not, and splitting it per tab would mean five files
 * that each re-derive the same error vocabulary.
 *
 * Two habits run through it. First, **nothing is patched optimistically**: an
 * admin action changes server-side arithmetic (a count, a queue, a sweep's
 * verdict), the round trip is cheap, and a table that briefly disagrees with
 * the server is worse than one that lags by 200 ms. Second, **every operational
 * POST answers with a job row, not a result** — the server queues the work
 * (`arc/api/acquisition.py`, `arc/api/retention.py`) — so the pages say
 * "queued", never "done".
 *
 * The one exception is pause/resume, which is a `settings` row and answers with
 * the flag as it now stands: the button is a toggle, and the sidebar's
 * "Acquisition paused" note has to agree with it, so both share
 * `acquisitionStatusQueryKey`.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { ApiError, apiFetch } from '@/lib/api'
import { acquisitionStatusQueryKey, type AcquisitionStatus } from '@/lib/acquisition'
import { ANIME_QUERY_KEY } from '@/lib/anime'
import { errorDetail, type Role } from '@/lib/auth'

/**
 * Byte sizes and coarse timestamps read the same everywhere in Arc, so the
 * admin page borrows the two formatters that already exist rather than growing
 * a second pair that rounds differently. Re-exported so a tab has one import.
 */
export { formatSize as formatBytes } from '@/lib/review'
export { formatRelativeTime } from '@/lib/recs'

/* --- Wire shapes: users and invites (FR-D1) --------------------------- */

/** `GET /api/users` — the server's `UserAdminOut`. */
export interface AdminAccount {
  id: number
  email: string
  role: Role
  timezone: string
  created_at: string
  is_active: boolean
}

/** `PATCH /api/users/{id}` — omitted fields are left alone. */
export interface UserPatch {
  is_active?: boolean
  role?: Role
}

/** Where an invite stands, as the server computes it. */
export type InviteStatus = 'pending' | 'used' | 'expired'

/** `GET /api/invites` — the server's `InviteOut`. Never carries a token. */
export interface InviteRow {
  id: number
  email: string | null
  created_by: number | null
  created_at: string
  expires_at: string
  used_at: string | null
  status: InviteStatus
}

/** `POST /api/invites` body. A null address leaves the link open to anyone. */
export interface InviteCreate {
  email: string | null
  expires_in_hours: number
}

/**
 * `POST /api/invites` response — the only sight of the token there will ever
 * be. The server stores `sha256(token)`, so an admin who navigates away
 * without copying the link has to issue a new invite.
 */
export interface InviteCreated {
  id: number
  email: string | null
  expires_at: string
  token: string
  url: string
}

/** The server's own default and ceiling for `expires_in_hours`. */
export const DEFAULT_EXPIRY_HOURS = 168
export const MAX_EXPIRY_HOURS = 720

/* --- Wire shapes: rules (FR-D2, FR-T5) -------------------------------- */

/**
 * The editable rules, keyed exactly as `settings` stores them
 * (`arc/models/settings.py`), because a client-side rename is one more place
 * for the two to drift.
 */
export interface SettingsValues {
  preferred_groups: string[]
  preferred_resolution: string | null
  fallback_resolution: string | null
  look_ahead_n: number
  grace_days_g: number
  unwatched_days_d: number
  sub_lang: string
  audio_lang: string
  acquisition_paused: boolean
}

export type SettingsKey = keyof SettingsValues

/** One show's rule override (`GET /api/settings`). Editing arrives in M16. */
export interface SettingsOverride {
  anime_id: number
  title: string
  preferred_groups: string[] | null
  resolution: string | null
}

/** `GET /api/settings` and the answer to `PUT /api/settings`. */
export interface SettingsPayload {
  values: SettingsValues
  /** First-boot values, shown as hints and restored by "reset to default". */
  defaults: SettingsValues
  overrides: SettingsOverride[]
}

/**
 * The bounds `PUT /api/settings` validates against: `MAX_LOOK_AHEAD` = 10 and
 * `MAX_DAYS` = 365, both read by `arc/services/settings.py`. Note that this is
 * *not* `arc/services/retention/rules.py`'s own `MAX_DAYS` (3650), which is a
 * defensive clamp on a value already in the table rather than a limit on what
 * may be written.
 *
 * They become `min`/`max` on the inputs, so the browser refuses an
 * out-of-range value before a request is made; the server's 422 stays the
 * authority for everything a number field cannot express.
 */
export const NUMBER_BOUNDS: Record<
  'look_ahead_n' | 'grace_days_g' | 'unwatched_days_d',
  {
    min: number
    max: number
  }
> = {
  look_ahead_n: { min: 0, max: 10 },
  grace_days_g: { min: 0, max: 365 },
  unwatched_days_d: { min: 0, max: 365 },
}

/** "0–365", for a hint under the field that carries the same numbers. */
export function rangeLabel(key: keyof typeof NUMBER_BOUNDS): string {
  const { min, max } = NUMBER_BOUNDS[key]
  return `${String(min)}–${String(max)}`
}

/** What the ranker knows how to prefer (`arc/services/acquisition/rules.py`). */
export const RESOLUTIONS = ['2160p', '1080p', '720p', '480p'] as const

/* --- Wire shapes: jobs (FR-D3) ---------------------------------------- */

export type JobStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

export const JOB_STATUSES: readonly JobStatus[] = [
  'pending',
  'running',
  'done',
  'failed',
  'cancelled',
]

/** `GET /api/jobs` — the server's `JobOut`. */
export interface JobRow {
  id: number
  type: string
  payload: Record<string, unknown>
  status: JobStatus
  priority: number
  attempts: number
  max_attempts: number
  run_after: string
  locked_by: string | null
  locked_at: string | null
  last_error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

/** `GET /api/jobs/summary` — the strip above the table. */
export interface JobsSummary {
  by_status: Record<JobStatus, number>
  /** Queue depth per type; the only breakdown that says *what* is backed up. */
  by_type_pending: Record<string, number>
  worker: {
    heartbeat_at: string | null
    /** The server's own verdict, so "alive" means one thing in both halves. */
    alive: boolean
  }
}

/** What the jobs table is currently asking for. `'all'` means "do not filter". */
export interface JobFilter {
  status: JobStatus | 'all'
  /** A job type, or the literal `'all'`. Not a union: the registry is open. */
  type: string
  offset: number
}

/** One screen of jobs. Deliberately small: this is a diagnostic, not a log. */
export const JOBS_PAGE_SIZE = 50

/**
 * How often the jobs tab re-asks while it is open. A job moves on the worker's
 * clock, not the admin's, so the only way to watch one is to poll; fifteen
 * seconds is what the show page already uses for acquisition (FR-A7).
 */
export const JOBS_REFETCH_MS = 15_000

/* --- Wire shapes: storage (FR-D3, FR-T4) ------------------------------ */

/** A filesystem's three numbers, in bytes. */
export interface DiskUsage {
  total: number
  used: number
  free: number
}

/** `GET /api/retention/disk`. */
export interface RetentionDisk {
  data_dir: DiskUsage
  retained: {
    sources: number
    renditions: number
    total: number
  }
  episodes_retained: number
}

/** One episode the next sweep would delete (`RetentionItemOut`). */
export interface RetentionItem {
  episode_id: number
  anime_id: number
  anime_title: string
  number: number
  state: string
  /** The sentence the sweep will log: which rule applied, and when G ran out. */
  reason: string
  bytes: number
  rendition_dir: string | null
  source_dir: string | null
  torrents: string[]
}

/** `GET /api/retention/preview` — the sweep's own rule, with no deletion. */
export interface RetentionPreview {
  /** `RETENTION_DRY_RUN`: the sweep will report this list and delete none. */
  dry_run: boolean
  episodes: RetentionItem[]
  bytes: number
}

/* --- Wire shapes: acquisition (FR-D3) --------------------------------- */

/** One live want (`WantOut`), with enough context to read the row. */
export interface WantRow {
  user_id: number
  user_email: string | null
  episode_id: number
  episode_number: number
  anime_id: number
  anime_title: string
  state: string
  unavailable_reason: string | null
}

/** One torrent as qBittorrent reports it, with Arc's episode attached. */
export interface QbitTorrent {
  hash: string
  name: string
  state: string
  /** 0–1, as qBittorrent gives it. */
  progress: number
  size: number
  dlspeed: number
  upspeed: number
  episode_id: number | null
}

/**
 * `GET /api/acquisition/qbit`. Unreachable is an answer, not an error: the
 * client renders `error` in place of the table rather than the page failing,
 * because "qBittorrent is down" is exactly what an admin came here to learn.
 */
export interface QbitStatus {
  reachable: boolean
  version: string | null
  error: string | null
  torrents: QbitTorrent[]
}

/** `POST /api/acquisition/{pause,resume}` — the flag as it now stands. */
export interface PauseResult {
  paused: boolean
}

/* --- Tabs ------------------------------------------------------------- */

/** The five tabs, in the order they are shown; `?tab=` holds one of them. */
export const ADMIN_TABS = ['users', 'rules', 'jobs', 'storage', 'acquisition'] as const

export type AdminTab = (typeof ADMIN_TABS)[number]

export const ADMIN_TAB_LABELS: Record<AdminTab, string> = {
  users: 'Users',
  rules: 'Rules',
  jobs: 'Jobs',
  storage: 'Storage',
  acquisition: 'Acquisition',
}

/**
 * `?tab=` as one of the five. Anything else — a typo, an old link, no value at
 * all — reads as the first tab: a mistyped query string should show the page,
 * not a blank one.
 */
export function adminTabFrom(raw: string | null): AdminTab {
  return ADMIN_TABS.find((tab) => tab === raw) ?? 'users'
}

/* --- Query keys ------------------------------------------------------- */

export const ADMIN_QUERY_KEY = 'admin'

export const adminUsersQueryKey = [ADMIN_QUERY_KEY, 'users'] as const
export const adminInvitesQueryKey = [ADMIN_QUERY_KEY, 'invites'] as const
export const adminSettingsQueryKey = [ADMIN_QUERY_KEY, 'settings'] as const
/** Prefix over both the listing and the summary, so one invalidate does both. */
export const adminJobsQueryKey = [ADMIN_QUERY_KEY, 'jobs'] as const
export const adminJobsSummaryQueryKey = [...adminJobsQueryKey, 'summary'] as const
export const adminDiskQueryKey = [ADMIN_QUERY_KEY, 'disk'] as const
export const adminPreviewQueryKey = [ADMIN_QUERY_KEY, 'preview'] as const
export const adminWantsQueryKey = [ADMIN_QUERY_KEY, 'wants'] as const
export const adminQbitQueryKey = [ADMIN_QUERY_KEY, 'qbit'] as const

export function adminJobsListQueryKey(filter: JobFilter): readonly unknown[] {
  return [...adminJobsQueryKey, 'list', filter.status, filter.type, filter.offset]
}

/* --- Words ------------------------------------------------------------ */

/** The 409 the server answers when an admin aims at their own account. */
export const NO_SELF_DEACTIVATE = 'you cannot deactivate your own account'
export const NO_SELF_DEMOTE = 'you cannot change your own role'
export const LAST_ADMIN = 'at least one active admin is required'

/**
 * What to say when an admin action fails.
 *
 * The server's `detail` is preferred wherever it exists, because on this page
 * it is written for exactly this reader — "at least one active admin is
 * required" says more than any sentence invented here could. Only the statuses
 * that have no useful detail get words of their own.
 */
export function adminErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not reach the server. Try again.'

  const detail = errorDetail(error)
  switch (error.status) {
    case 401:
      return 'Your session expired. Sign in again.'
    case 403:
      return 'That needs an administrator account.'
    case 404:
      return detail ?? 'That is no longer there. Refresh to see the current state.'
    case 409:
      return detail ?? 'That cannot be done right now.'
    case 422:
      return detail ?? 'Some of those values were rejected. Check the fields below.'
    case 502:
    case 503:
      return detail ?? 'A service Arc depends on is not answering. Try again in a moment.'
    default:
      return 'Something went wrong. Try again.'
  }
}

/**
 * A 422's per-field messages, keyed by field name.
 *
 * Two shapes are accepted because two are possible: FastAPI's own validation
 * error (`detail` is a list of `{loc, msg}`, where `loc` is `["body", "field"]`)
 * and a hand-written `detail` object mapping field to sentence. Anything else —
 * a plain string detail, a 500's HTML — yields no field errors, and the caller
 * falls back to `adminErrorMessage`.
 */
export function settingsFieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || error.status !== 422) return {}

  const body: unknown = error.body
  if (typeof body !== 'object' || body === null || !('detail' in body)) return {}
  const detail: unknown = body.detail

  const errors: Record<string, string> = {}

  if (Array.isArray(detail)) {
    for (const entry of detail as readonly unknown[]) {
      if (typeof entry !== 'object' || entry === null) continue
      const loc: unknown = 'loc' in entry ? entry.loc : null
      const msg: unknown = 'msg' in entry ? entry.msg : null
      if (typeof msg !== 'string') continue
      // The last string segment names the field; "body" and the array indices
      // in between are routing, not something to show a person.
      const field = stringParts(loc)
        .filter((part) => part !== 'body')
        .pop()
      if (field !== undefined) errors[field] = msg
    }
    return errors
  }

  if (typeof detail === 'object' && detail !== null) {
    for (const [field, message] of Object.entries(detail as Record<string, unknown>)) {
      if (typeof message === 'string') errors[field] = message
    }
  }
  return errors
}

/** The string members of something that may or may not be a list. */
function stringParts(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const parts: string[] = []
  for (const part of value as readonly unknown[]) {
    if (typeof part === 'string') parts.push(part)
  }
  return parts
}

/* --- Rules arithmetic ------------------------------------------------- */

function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, index) => item === b[index])
  }
  return a === b
}

/**
 * Only what the admin actually changed.
 *
 * The rules form is bound to the whole object, but a PUT that sends every key
 * would overwrite a value another admin changed in the meantime with whatever
 * this form was loaded with. Sending the differences alone makes two admins
 * editing different fields a non-event.
 */
export function changedSettings(
  edited: SettingsValues,
  loaded: SettingsValues,
): Partial<SettingsValues> {
  const changed: Partial<SettingsValues> = {}
  for (const key of Object.keys(loaded) as SettingsKey[]) {
    if (!sameValue(edited[key], loaded[key])) {
      // Each key is assigned from its own value, so the union stays sound.
      Object.assign(changed, { [key]: edited[key] })
    }
  }
  return changed
}

/** How a default reads under a field. Arrays and nulls both need words. */
export function defaultLabel(value: SettingsValues[SettingsKey]): string {
  if (Array.isArray(value)) return value.length === 0 ? 'none' : value.join(', ')
  if (value === null || value === '') return 'none'
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  return String(value)
}

/* --- Formatting ------------------------------------------------------- */

/**
 * The date part of a timestamp, as the server sent it.
 *
 * Deliberately not localised: "2026-09-01" is unambiguous in every locale, an
 * account's creation date is never read to the minute, and a relative form
 * ("11 months ago") is the wrong answer for a column people scan for order.
 */
export function isoDate(iso: string | null): string {
  if (iso === null) return '—'
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return iso
  return iso.slice(0, 10)
}

/** "62%" from a 0–1 fraction; a nonsense number reads as 0. */
export function formatPercent(fraction: number): string {
  if (!Number.isFinite(fraction)) return '0%'
  return `${String(Math.round(Math.min(1, Math.max(0, fraction)) * 100))}%`
}

/** How full the data disk is, 0–1. An empty or unknown disk reads as 0. */
export function usedFraction(disk: DiskUsage): number {
  if (!Number.isFinite(disk.total) || disk.total <= 0) return 0
  return Math.min(1, Math.max(0, disk.used / disk.total))
}

/** "3/5" — attempts against the ceiling, which is what decides a retry. */
export function attemptsLabel(job: JobRow): string {
  return `${String(job.attempts)}/${String(job.max_attempts)}`
}

/** Retry is for work that has stopped and did not finish (server: 409 else). */
export function canRetry(job: JobRow): boolean {
  return job.status === 'failed' || job.status === 'cancelled'
}

/** Cancel is for work that has not started; a running job is left alone. */
export function canCancel(job: JobRow): boolean {
  return job.status === 'pending'
}

/* --- Queries ---------------------------------------------------------- */

/**
 * Every list on this page is small, admin-only, and changed by the admin
 * looking at it, so they share one shape: no retry (a failed load offers a
 * button instead of a storm) and no automatic refetch beyond the default.
 */
function useAdminQuery<T>(key: readonly unknown[], path: string): UseQueryResult<T, Error> {
  return useQuery<T, Error>({
    queryKey: key,
    queryFn: () => apiFetch<T>(path),
    retry: false,
  })
}

/** `GET /api/users` (FR-D1). */
export function useAdminUsers(): UseQueryResult<AdminAccount[], Error> {
  return useAdminQuery<AdminAccount[]>(adminUsersQueryKey, '/api/users')
}

/** `GET /api/invites` (FR-D1). Open, used and expired, newest first. */
export function useInvites(): UseQueryResult<InviteRow[], Error> {
  return useAdminQuery<InviteRow[]>(adminInvitesQueryKey, '/api/invites')
}

/** `GET /api/settings` (FR-D2, FR-T5): the values, their defaults, overrides. */
export function useSettings(): UseQueryResult<SettingsPayload, Error> {
  return useAdminQuery<SettingsPayload>(adminSettingsQueryKey, '/api/settings')
}

/** `GET /api/jobs/summary` — polled with the table it sits above. */
export function useJobsSummary(): UseQueryResult<JobsSummary, Error> {
  return useQuery<JobsSummary, Error>({
    queryKey: adminJobsSummaryQueryKey,
    queryFn: () => apiFetch<JobsSummary>('/api/jobs/summary'),
    retry: false,
    refetchInterval: JOBS_REFETCH_MS,
  })
}

/** The query string for one filter. Ordered, so a test can name the URL. */
export function jobsPath(filter: JobFilter): string {
  const params = new URLSearchParams()
  if (filter.status !== 'all') params.set('status', filter.status)
  if (filter.type !== 'all') params.set('type', filter.type)
  params.set('limit', String(JOBS_PAGE_SIZE))
  params.set('offset', String(filter.offset))
  return `/api/jobs?${params.toString()}`
}

/**
 * One filtered page of the queue.
 *
 * `keepPreviousData` so changing a filter or stepping a page does not blank
 * the table under the control that did it — and, more to the point, so the
 * fifteen-second poll never flashes an empty table between requests.
 */
export function useJobs(filter: JobFilter): UseQueryResult<JobRow[], Error> {
  return useQuery<JobRow[], Error>({
    queryKey: adminJobsListQueryKey(filter),
    queryFn: () => apiFetch<JobRow[]>(jobsPath(filter)),
    retry: false,
    placeholderData: keepPreviousData,
    refetchInterval: JOBS_REFETCH_MS,
  })
}

/** `GET /api/retention/disk` (FR-D3, FR-T4). */
export function useRetentionDisk(): UseQueryResult<RetentionDisk, Error> {
  return useAdminQuery<RetentionDisk>(adminDiskQueryKey, '/api/retention/disk')
}

/** `GET /api/retention/preview` — what the next sweep would delete, and why. */
export function useRetentionPreview(): UseQueryResult<RetentionPreview, Error> {
  return useAdminQuery<RetentionPreview>(adminPreviewQueryKey, '/api/retention/preview')
}

/** `GET /api/acquisition/wants` — the whole want table (FR-A2). */
export function useWants(): UseQueryResult<WantRow[], Error> {
  return useAdminQuery<WantRow[]>(adminWantsQueryKey, '/api/acquisition/wants')
}

/** `GET /api/acquisition/qbit` — reachable or not, and what it is holding. */
export function useQbit(): UseQueryResult<QbitStatus, Error> {
  return useAdminQuery<QbitStatus>(adminQbitQueryKey, '/api/acquisition/qbit')
}

/** The acquisition counts, re-exported here so the tab has one import. */
export { useAcquisitionStatus } from '@/lib/acquisition'
export type { AcquisitionStatus }

/* --- Mutations -------------------------------------------------------- */

/** Deactivate, reactivate, promote or demote one account (FR-D1). */
export function useUpdateUser(): UseMutationResult<
  AdminAccount,
  Error,
  { id: number; patch: UserPatch }
> {
  const client = useQueryClient()

  return useMutation<AdminAccount, Error, { id: number; patch: UserPatch }>({
    mutationFn: ({ id, patch }) =>
      apiFetch<AdminAccount>(`/api/users/${String(id)}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminUsersQueryKey })
    },
  })
}

/**
 * Issue an invite link (FR-D1). The result is kept by the caller rather than
 * in the cache: it holds the one-time token, and a token that survives in a
 * query cache is a token that comes back when the page is revisited.
 */
export function useCreateInvite(): UseMutationResult<InviteCreated, Error, InviteCreate> {
  const client = useQueryClient()

  return useMutation<InviteCreated, Error, InviteCreate>({
    mutationFn: (body) =>
      apiFetch<InviteCreated>('/api/invites', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminInvitesQueryKey })
    },
  })
}

/** Revoke an invite. The row survives and reads as expired from then on. */
export function useDeleteInvite(): UseMutationResult<void, Error, number> {
  const client = useQueryClient()

  return useMutation<void, Error, number>({
    mutationFn: (id) => apiFetch<void>(`/api/invites/${String(id)}`, { method: 'DELETE' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminInvitesQueryKey })
    },
  })
}

/**
 * Write the changed rules (FR-D2, FR-T5).
 *
 * The answer is the whole payload, so it is written straight into the cache:
 * the form rebinds to what the server now holds rather than to what was typed,
 * which is the difference between "saved" and "probably saved". Acquisition
 * status is invalidated with it because `acquisition_paused` lives in the same
 * table and the sidebar note reads it.
 */
export function useSaveSettings(): UseMutationResult<
  SettingsPayload,
  Error,
  Partial<SettingsValues>
> {
  const client = useQueryClient()

  return useMutation<SettingsPayload, Error, Partial<SettingsValues>>({
    mutationFn: (patch) =>
      apiFetch<SettingsPayload>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),
    onSuccess: (payload) => {
      client.setQueryData(adminSettingsQueryKey, payload)
      void client.invalidateQueries({ queryKey: acquisitionStatusQueryKey })
    },
  })
}

/** Both job actions answer with the row, and both move the summary counts. */
function useJobAction(action: 'retry' | 'cancel'): UseMutationResult<JobRow, Error, number> {
  const client = useQueryClient()

  return useMutation<JobRow, Error, number>({
    mutationFn: (id) => apiFetch<JobRow>(`/api/jobs/${String(id)}/${action}`, { method: 'POST' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminJobsQueryKey })
    },
  })
}

/** Put a failed or cancelled job back in the queue. 409 when it is neither. */
export function useRetryJob(): UseMutationResult<JobRow, Error, number> {
  return useJobAction('retry')
}

/** Stop a pending job from ever running. 409 once it has started. */
export function useCancelJob(): UseMutationResult<JobRow, Error, number> {
  return useJobAction('cancel')
}

/**
 * Everything under Storage queues a job and changes what the sweep would do
 * next, so all three invalidate the same four things. The anime tree goes with
 * them: a show page left open must not go on claiming a state that a deletion
 * has just reset (FR-T3).
 */
function useStorageAction(path: (id: number) => string): UseMutationResult<JobRow, Error, number> {
  const client = useQueryClient()

  return useMutation<JobRow, Error, number>({
    mutationFn: (id) => apiFetch<JobRow>(path(id), { method: 'POST' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminJobsQueryKey })
      void client.invalidateQueries({ queryKey: adminPreviewQueryKey })
      void client.invalidateQueries({ queryKey: adminDiskQueryKey })
      void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY] })
    },
  })
}

/** Run the hourly sweep now (FR-T4). The same job, under the same dedupe key. */
export function useRunSweep(): UseMutationResult<JobRow, Error, void> {
  const client = useQueryClient()

  return useMutation<JobRow, Error, void>({
    mutationFn: () => apiFetch<JobRow>('/api/retention/sweep', { method: 'POST' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminJobsQueryKey })
      void client.invalidateQueries({ queryKey: adminPreviewQueryKey })
      void client.invalidateQueries({ queryKey: adminDiskQueryKey })
      void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY] })
    },
  })
}

/** Delete one episode's files now, grace period or not (FR-T4). */
export function useDeleteEpisodeFiles(): UseMutationResult<JobRow, Error, number> {
  return useStorageAction((id) => `/api/episodes/${String(id)}/delete-files`)
}

/** Queue a fresh Nyaa search for one episode — FR-T4's "or re-fetch". */
export function useRefetchEpisode(): UseMutationResult<JobRow, Error, number> {
  return useStorageAction((id) => `/api/episodes/${String(id)}/search`)
}

/**
 * The acquisition kill switch (FR-A1's brake).
 *
 * Two endpoints behind one boolean, because from the page it is one switch.
 * Resume queues a `compute_wants` server-side, so the jobs view is invalidated
 * as well — otherwise the queue would look unchanged after the one press that
 * definitely changed it.
 */
export function useSetPaused(): UseMutationResult<PauseResult, Error, boolean> {
  const client = useQueryClient()

  return useMutation<PauseResult, Error, boolean>({
    mutationFn: (paused) =>
      apiFetch<PauseResult>(`/api/acquisition/${paused ? 'pause' : 'resume'}`, { method: 'POST' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: acquisitionStatusQueryKey })
      void client.invalidateQueries({ queryKey: adminSettingsQueryKey })
      void client.invalidateQueries({ queryKey: adminWantsQueryKey })
      void client.invalidateQueries({ queryKey: adminJobsQueryKey })
    },
  })
}
