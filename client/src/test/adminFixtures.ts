/**
 * Fixtures for the admin page (roadmap M14).
 *
 * Shaped exactly like the server's responses — `UserAdminOut`, `InviteOut`,
 * `JobOut`, `RetentionPreviewOut`, `WantOut` and the M14 additions — so a test
 * that passes here is a test against the wire, not against a convenient
 * invention. Ids are spread far apart (users in the single digits, invites in
 * the tens, jobs in the hundreds) so that a `PATCH /api/users/5` in an
 * assertion can only mean one thing.
 */

import type {
  AdminAccount,
  InviteRow,
  JobRow,
  JobsSummary,
  QbitStatus,
  RetentionDisk,
  RetentionPreview,
  SettingsPayload,
  SettingsValues,
  WantRow,
} from '@/lib/admin'

/* --- Users and invites ------------------------------------------------ */

/** The viewer: `TEST_ADMIN`'s id, so "(you)" lands on this row. */
export const ADMIN_SELF: AdminAccount = {
  id: 2,
  email: 'admin@example.com',
  role: 'admin',
  timezone: 'Europe/Berlin',
  created_at: '2026-08-01T09:00:00Z',
  is_active: true,
}

export const OTHER_USER: AdminAccount = {
  id: 5,
  email: 'leah@example.com',
  role: 'user',
  timezone: 'Europe/Berlin',
  created_at: '2026-08-14T09:00:00Z',
  is_active: true,
}

export const DISABLED_USER: AdminAccount = {
  id: 6,
  email: 'sam@example.com',
  role: 'user',
  timezone: 'UTC',
  created_at: '2026-08-20T09:00:00Z',
  is_active: false,
}

export const ACCOUNTS: AdminAccount[] = [ADMIN_SELF, OTHER_USER, DISABLED_USER]

export const PENDING_INVITE: InviteRow = {
  id: 11,
  email: 'newcomer@example.com',
  created_by: 2,
  created_at: '2026-09-08T10:00:00Z',
  expires_at: '2026-09-15T10:00:00Z',
  used_at: null,
  status: 'pending',
}

export const USED_INVITE: InviteRow = {
  id: 12,
  email: 'leah@example.com',
  created_by: 2,
  created_at: '2026-08-14T08:00:00Z',
  expires_at: '2026-08-21T08:00:00Z',
  used_at: '2026-08-14T09:00:00Z',
  status: 'used',
}

export const INVITES: InviteRow[] = [PENDING_INVITE, USED_INVITE]

export const CREATED_INVITE_URL = 'https://arc.example.com/invite/tok-abc123'

export const CREATED_INVITE = {
  id: 13,
  email: 'newcomer@example.com',
  expires_at: '2026-09-17T10:00:00Z',
  token: 'tok-abc123',
  url: CREATED_INVITE_URL,
}

/* --- Rules ------------------------------------------------------------ */

/** The seeded first-boot values (`arc/models/settings.py`). */
export const SETTINGS_DEFAULTS: SettingsValues = {
  preferred_groups: [],
  preferred_resolution: '1080p',
  fallback_resolution: '720p',
  look_ahead_n: 2,
  grace_days_g: 7,
  unwatched_days_d: 21,
  sub_lang: 'en',
  audio_lang: 'ja',
  acquisition_paused: false,
}

/** What this server actually holds: three of them moved off the default. */
export const SETTINGS_VALUES: SettingsValues = {
  ...SETTINGS_DEFAULTS,
  preferred_groups: ['SubsPlease', 'Erai-raws'],
  look_ahead_n: 3,
}

export const SETTINGS: SettingsPayload = {
  values: SETTINGS_VALUES,
  defaults: SETTINGS_DEFAULTS,
  overrides: [
    {
      anime_id: 700,
      title: 'Sousou no Frieren',
      preferred_groups: ['Tsundere-Raws'],
      resolution: '2160p',
    },
  ],
}

/* --- Jobs ------------------------------------------------------------- */

function job(overrides: Partial<JobRow>): JobRow {
  return {
    id: 100,
    type: 'compute_wants',
    payload: {},
    status: 'pending',
    priority: 100,
    attempts: 0,
    max_attempts: 5,
    run_after: '2026-09-10T11:59:00Z',
    locked_by: null,
    locked_at: null,
    last_error: null,
    created_at: '2026-09-10T11:58:00Z',
    started_at: null,
    finished_at: null,
    ...overrides,
  }
}

export const PENDING_JOB = job({ id: 101, type: 'compute_wants', status: 'pending' })

/** Long enough that the table has to truncate it — that is the point of it. */
export const LONG_ERROR =
  'ffmpeg exited with 1: [libx264 @ 0x5586] height not divisible by 2 (1079); ' +
  'conversion failed after 3 frames while burning subtitles from stream 0:2 of the source file'

export const FAILED_JOB = job({
  id: 102,
  type: 'transcode',
  status: 'failed',
  attempts: 3,
  last_error: LONG_ERROR,
  started_at: '2026-09-10T11:40:00Z',
  finished_at: '2026-09-10T11:45:00Z',
})

export const RUNNING_JOB = job({
  id: 103,
  type: 'search_release',
  status: 'running',
  attempts: 1,
  locked_by: 'worker-1',
  started_at: '2026-09-10T11:58:30Z',
})

export const JOBS: JobRow[] = [RUNNING_JOB, FAILED_JOB, PENDING_JOB]

export const JOBS_SUMMARY: JobsSummary = {
  by_status: { pending: 4, running: 1, done: 812, failed: 2, cancelled: 0 },
  by_type_pending: { compute_wants: 1, search_release: 3 },
  worker: { heartbeat_at: '2026-09-10T11:59:50Z', alive: true },
}

/* --- Storage ---------------------------------------------------------- */

export const DISK: RetentionDisk = {
  data_dir: { total: 100_000_000_000, used: 62_000_000_000, free: 38_000_000_000 },
  retained: { sources: 40_000_000_000, renditions: 18_000_000_000, total: 58_000_000_000 },
  episodes_retained: 27,
}

export const RETENTION_REASON = 'watched 9 days ago; grace of 7 days ran out 2 days ago'

export const RETENTION_PREVIEW: RetentionPreview = {
  dry_run: false,
  bytes: 3_200_000_000,
  episodes: [
    {
      episode_id: 9005,
      anime_id: 700,
      anime_title: 'Sousou no Frieren',
      number: 5,
      state: 'ready',
      reason: RETENTION_REASON,
      bytes: 3_200_000_000,
      rendition_dir: '/data/renditions/9005',
      source_dir: '/data/library/frieren',
      torrents: ['abc123'],
    },
  ],
}

/* --- Acquisition ------------------------------------------------------ */

export const WANTS: WantRow[] = [
  {
    user_id: 5,
    user_email: 'leah@example.com',
    episode_id: 9006,
    episode_number: 6,
    anime_id: 700,
    anime_title: 'Sousou no Frieren',
    state: 'downloading',
    unavailable_reason: null,
  },
  {
    user_id: 2,
    user_email: 'admin@example.com',
    episode_id: 9101,
    episode_number: 1,
    anime_id: 701,
    anime_title: 'Dungeon Meshi',
    state: 'unavailable',
    unavailable_reason: 'no release matched the rules after 6 attempts',
  },
]

export const QBIT: QbitStatus = {
  reachable: true,
  version: 'v4.6.5',
  error: null,
  torrents: [
    {
      hash: 'abc123',
      name: '[SubsPlease] Sousou no Frieren - 06 (1080p).mkv',
      state: 'downloading',
      progress: 0.42,
      size: 1_400_000_000,
      dlspeed: 5_200_000,
      upspeed: 0,
      episode_id: 9006,
    },
  ],
}

export const QBIT_DOWN: QbitStatus = {
  reachable: false,
  version: null,
  error: 'connection refused: qbittorrent:8080',
  torrents: [],
}
