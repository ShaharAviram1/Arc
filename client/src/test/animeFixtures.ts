import type {
  AnimeDetail,
  AnimeRelation,
  AnimeSearchResponse,
  AnimeSummary,
  EpisodeOut,
  EpisodeRelease,
  EpisodeRendition,
  ListEntry,
} from '@/lib/anime'
import type { MalStatus, MalWrite } from '@/lib/mal'
import type { PlayInfo } from '@/lib/playback'
import type { RecContinuation, RecModelStatus, RecPick, RecRun, RecsPage } from '@/lib/recs'
import type {
  BehindEntry,
  ContinueWatchingEntry,
  HomePage,
  ScheduleDay,
  ScheduleEntry,
  SchedulePage,
} from '@/lib/schedule'

export const FRIEREN: AnimeSummary = {
  id: 154587,
  title: {
    romaji: 'Sousou no Frieren',
    english: 'Frieren: Beyond Journey’s End',
    native: '葬送のフリーレン',
    preferred: 'Frieren: Beyond Journey’s End',
  },
  format: 'TV',
  episodes: 28,
  status: 'FINISHED',
  season: 'FALL',
  season_year: 2023,
  cover_url: 'https://example.test/frieren.jpg',
  anilist_id: 154587,
  mal_id: 52991,
  source: 'anilist',
  list_status: null,
}

/**
 * Second result: no cover and no episode count, the sparse-catalogue case —
 * and the one AniList has never filled, so it carries the MAL source marker.
 */
export const FRIEREN_SPECIAL: AnimeSummary = {
  id: 176496,
  title: {
    romaji: 'Sousou no Frieren: ●● no Mahou',
    english: null,
    native: null,
    preferred: 'Sousou no Frieren: ●● no Mahou',
  },
  format: 'SPECIAL',
  episodes: null,
  status: 'FINISHED',
  season: null,
  season_year: null,
  cover_url: null,
  anilist_id: null,
  mal_id: 58592,
  source: 'mal',
  list_status: 'planned',
}

/** A relation Arc already has a row for: `id` is set, so the show page links it. */
export const LINKED_RELATION: AnimeRelation = {
  id: FRIEREN_SPECIAL.id,
  anilist_id: FRIEREN_SPECIAL.anilist_id,
  mal_id: FRIEREN_SPECIAL.mal_id,
  relation_type: 'SIDE_STORY',
  title: FRIEREN_SPECIAL.title,
  format: 'SPECIAL',
}

/**
 * A relation nothing has pulled into the catalogue yet: AniList and MAL know
 * it, Arc has no row, so `id` is null and the show page has nowhere to link.
 */
export const UNLINKED_RELATION: AnimeRelation = {
  id: null,
  anilist_id: 189327,
  mal_id: 61121,
  relation_type: 'SEQUEL',
  title: {
    romaji: 'Sousou no Frieren 2nd Season',
    english: null,
    native: '葬送のフリーレン 第2期',
    preferred: 'Sousou no Frieren 2nd Season',
  },
  format: 'TV',
}

export const SEARCH_PAGE_1: AnimeSearchResponse = {
  results: [FRIEREN, FRIEREN_SPECIAL],
  page: 1,
  has_next: true,
}

export const EMPTY_SEARCH: AnimeSearchResponse = { results: [], page: 1, has_next: false }

/** The release the ranked rules picked for the episode being downloaded (FR-A3). */
export const CHOSEN_RELEASE: EpisodeRelease = {
  group: 'SubsPlease',
  resolution: '1080p',
  title: '[SubsPlease] Sousou no Frieren - 04 (1080p) [A1B2C3D4].mkv',
  seeders: 123,
}

/** Why episode 5 gave up, after the FR-A6 retry window closed. */
export const UNAVAILABLE_REASON = 'No acceptable release found after 14 days.'

/** What episode 1's transcode produced: 1080p, English subs over a Japanese track. */
export const READY_RENDITION: EpisodeRendition = {
  duration: 1436.8,
  width: 1920,
  height: 1080,
  subtitle_lang: 'en',
  audio_lang: 'ja',
}

/** The tail of ffmpeg's own complaint about episode 7 (FR-P4). */
export const FAILURE_REASON =
  'ffmpeg exited 1: [matroska @ 0x5] Invalid data found when processing input'

/** How far episode 2's transcode has got (FR-P4). */
export const PREPARE_PROGRESS = 0.3

/**
 * One episode in each shape the show page has to render: ready (playable, with
 * the rendition it produced), mid-pipeline, unaired / not wanted, the three
 * acquisition states of FR-A7 — downloading with a percentage and a chosen
 * release, unavailable with a reason, and searching with neither — and the two
 * transcode states of FR-P4: preparing with a percentage, failed with ffmpeg's
 * reason.
 */
export const FRIEREN_DETAIL: AnimeDetail = {
  ...FRIEREN,
  episodes: [
    {
      id: 9001,
      number: 1,
      title: 'The Journey’s End',
      air_at: '2023-09-29T14:00:00Z',
      air_at_estimated: false,
      aired: true,
      state: 'ready',
      watched: true,
      download_progress: null,
      prepare_progress: null,
      failure_reason: null,
      unavailable_reason: null,
      release: null,
      rendition: READY_RENDITION,
    },
    {
      id: 9002,
      number: 2,
      title: null,
      air_at: '2023-10-06T14:00:00Z',
      air_at_estimated: false,
      aired: true,
      state: 'preparing',
      watched: false,
      download_progress: null,
      prepare_progress: PREPARE_PROGRESS,
      failure_reason: null,
      unavailable_reason: null,
      release: null,
      rendition: null,
    },
    {
      id: 9003,
      number: 3,
      title: 'Killing Magic',
      air_at: '2099-10-13T14:00:00Z',
      air_at_estimated: false,
      aired: false,
      state: 'not_wanted',
      watched: false,
      download_progress: null,
      prepare_progress: null,
      failure_reason: null,
      unavailable_reason: null,
      release: null,
      rendition: null,
    },
    {
      id: 9004,
      number: 4,
      title: 'The Land Where Souls Rest',
      air_at: '2023-10-20T14:00:00Z',
      air_at_estimated: false,
      aired: true,
      state: 'downloading',
      watched: false,
      download_progress: 0.42,
      prepare_progress: null,
      failure_reason: null,
      unavailable_reason: null,
      release: CHOSEN_RELEASE,
      rendition: null,
    },
    {
      id: 9005,
      number: 5,
      title: null,
      air_at: '2023-10-27T14:00:00Z',
      air_at_estimated: false,
      aired: true,
      state: 'unavailable',
      watched: false,
      download_progress: null,
      prepare_progress: null,
      failure_reason: null,
      unavailable_reason: UNAVAILABLE_REASON,
      release: null,
      rendition: null,
    },
    {
      id: 9006,
      number: 6,
      title: null,
      air_at: '2023-11-03T14:00:00Z',
      air_at_estimated: false,
      aired: true,
      state: 'searching',
      watched: false,
      download_progress: null,
      prepare_progress: null,
      failure_reason: null,
      unavailable_reason: null,
      release: null,
      rendition: null,
    },
    {
      id: 9007,
      number: 7,
      title: null,
      air_at: '2023-11-10T14:00:00Z',
      air_at_estimated: false,
      aired: true,
      state: 'failed',
      watched: false,
      download_progress: null,
      prepare_progress: null,
      failure_reason: FAILURE_REASON,
      unavailable_reason: null,
      release: null,
      rendition: null,
    },
  ],
  episode_count: 28,
  synopsis: 'The elf mage Frieren outlives her party.\nA new journey begins.',
  genres: ['Adventure', 'Drama', 'Fantasy'],
  studio: 'Madhouse',
  banner_url: null,
  next_airing: null,
  /** Both relation shapes, so the show page renders a linked and a plain one. */
  relations: [LINKED_RELATION, UNLINKED_RELATION],
  list_entry: null,
}

export const FRIEREN_DETAIL_ON_LIST: AnimeDetail = {
  ...FRIEREN_DETAIL,
  list_status: 'watching',
  list_entry: listEntry({ progress: 4, score: 9, updated_at: '2026-09-01T10:00:00Z' }),
}

/** A show AniList has no airing status for: the null-`status` render path. */
export const FRIEREN_DETAIL_NO_STATUS: AnimeDetail = {
  ...FRIEREN_DETAIL,
  status: null,
  title: { ...FRIEREN_DETAIL.title, romaji: null },
}

/**
 * The same show as it comes back while AniList is down (FR-C6): filled from
 * MyAnimeList, so there is no AniList id yet and every air date is synthesised
 * from the broadcast slot.
 */
export const FRIEREN_DETAIL_VIA_MAL: AnimeDetail = {
  ...FRIEREN_DETAIL,
  anilist_id: null,
  source: 'mal',
  episodes: FRIEREN_DETAIL.episodes.map((episode) => ({ ...episode, air_at_estimated: true })),
}

/**
 * The same show with nothing left in flight: every episode has either arrived
 * or was never wanted, so the show page has no reason to poll (FR-A7).
 */
export const FRIEREN_DETAIL_SETTLED: AnimeDetail = {
  ...FRIEREN_DETAIL,
  episodes: FRIEREN_DETAIL.episodes.map((episode, index) => ({
    ...episode,
    state: index === 0 ? 'ready' : 'not_wanted',
    download_progress: null,
    prepare_progress: null,
    failure_reason: null,
    unavailable_reason: null,
    release: null,
  })),
}

export function listEntry(overrides: Partial<ListEntry> = {}): ListEntry {
  return {
    anime_id: FRIEREN.id,
    status: 'watching',
    progress: 0,
    score: null,
    updated_at: '2026-09-06T09:00:00Z',
    ...overrides,
  }
}

/* --- Schedule and home (roadmap M4) ---------------------------------- */

/** A second currently-airing show, so a day column is not the same title twice. */
export const APOTHECARY: AnimeSummary = {
  id: 161645,
  title: {
    romaji: 'Kusuriya no Hitorigoto',
    english: 'The Apothecary Diaries',
    native: '薬屋のひとりごと',
    preferred: 'The Apothecary Diaries',
  },
  format: 'TV',
  episodes: 24,
  status: 'RELEASING',
  season: 'FALL',
  season_year: 2026,
  // No cover: the compact row has to fall back to the placeholder.
  cover_url: null,
  anilist_id: 161645,
  mal_id: 54492,
  source: 'anilist',
  list_status: null,
}

export function scheduleEntry(
  anime: AnimeSummary,
  overrides: Partial<Omit<ScheduleEntry, 'anime'>> = {},
): ScheduleEntry {
  return {
    anime,
    air_time_local: '18:30',
    next_episode: null,
    next_at: null,
    next_at_estimated: false,
    following: false,
    list_status: anime.list_status,
    ...overrides,
  }
}

/** Seven empty Monday-first columns, the shape the server always sends. */
function emptyDays(): ScheduleDay[] {
  return [0, 1, 2, 3, 4, 5, 6].map((weekday) => ({ weekday, entries: [] }))
}

/**
 * Frieren airs Tuesday and is followed, with a real AniList airing time; the
 * Apothecary airs Thursday off a MAL broadcast slot, so its time is a guess
 * and carries the "est." marker (FR-C6).
 */
export const SCHEDULE_PAGE: SchedulePage = {
  year: 2026,
  season: 'FALL',
  prev: { year: 2026, season: 'SUMMER' },
  next: { year: 2027, season: 'WINTER' },
  timezone: 'Europe/Berlin',
  days: emptyDays().map((day) => {
    if (day.weekday === 1) {
      return {
        ...day,
        entries: [
          scheduleEntry(FRIEREN, {
            air_time_local: '18:30',
            next_episode: 5,
            next_at: '2026-09-08T16:30:00Z',
            following: true,
            list_status: 'watching',
          }),
        ],
      }
    }
    if (day.weekday === 3) {
      return {
        ...day,
        entries: [
          scheduleEntry(APOTHECARY, {
            air_time_local: '22:00',
            next_episode: 2,
            next_at: '2026-09-10T20:00:00Z',
            next_at_estimated: true,
          }),
        ],
      }
    }
    return day
  }),
  unscheduled: [scheduleEntry(FRIEREN_SPECIAL, { air_time_local: null, list_status: 'planned' })],
}

/** A season the daily sweep has not filled yet. */
export const EMPTY_SCHEDULE: SchedulePage = {
  ...SCHEDULE_PAGE,
  days: emptyDays(),
  unscheduled: [],
}

function airedEpisode(overrides: Partial<EpisodeOut> = {}): EpisodeOut {
  return {
    id: 9101,
    number: 7,
    title: null,
    air_at: '2026-09-04T14:00:00Z',
    air_at_estimated: false,
    aired: true,
    state: 'ready',
    watched: false,
    download_progress: null,
    prepare_progress: null,
    failure_reason: null,
    unavailable_reason: null,
    release: null,
    rendition: null,
    ...overrides,
  }
}

/** Seven of twelve aired, three watched: behind by four. */
export const BEHIND_FRIEREN: BehindEntry = {
  anime: { ...FRIEREN, episodes: 12, status: 'RELEASING', list_status: 'watching' },
  entry: listEntry({ status: 'watching', progress: 3 }),
  aired: 7,
  behind: 4,
  latest_aired_at: '2026-09-04T14:00:00Z',
}

export const HOME_PAGE: HomePage = {
  continue_watching: [],
  behind: [BEHIND_FRIEREN],
  new_this_week: [
    { anime: FRIEREN, episode: airedEpisode() },
    {
      anime: APOTHECARY,
      // Filled from MAL while AniList was down, so the date is a guess (FR-C6).
      episode: airedEpisode({
        id: 9102,
        number: 2,
        air_at: '2026-09-05T15:00:00Z',
        air_at_estimated: true,
        state: 'preparing',
      }),
    },
  ],
}

/** One episode still coming down, so the row carries a percentage (FR-A7). */
export const HOME_PAGE_DOWNLOADING: HomePage = {
  ...HOME_PAGE,
  new_this_week: [
    {
      anime: FRIEREN,
      episode: airedEpisode({
        state: 'downloading',
        download_progress: 0.42,
        release: CHOSEN_RELEASE,
      }),
    },
  ],
}

/** One episode still being transcoded, so the row carries a percentage (FR-P4). */
export const HOME_PAGE_PREPARING: HomePage = {
  ...HOME_PAGE,
  new_this_week: [
    {
      anime: FRIEREN,
      episode: airedEpisode({ state: 'preparing', prepare_progress: PREPARE_PROGRESS }),
    },
  ],
}

/** Twelve and a half minutes into a twenty-four minute episode. */
export const CONTINUE_FRIEREN: ContinueWatchingEntry = {
  anime: FRIEREN,
  episode: airedEpisode({ id: 9201, number: 5 }),
  position_s: 754,
  duration_s: 1436,
}

export const HOME_PAGE_CONTINUE: HomePage = {
  ...HOME_PAGE,
  continue_watching: [CONTINUE_FRIEREN],
}

/** A report landed before anything recorded the episode's length. */
export const CONTINUE_NO_DURATION: ContinueWatchingEntry = {
  ...CONTINUE_FRIEREN,
  duration_s: null,
}

export const HOME_PAGE_CONTINUE_NO_DURATION: HomePage = {
  ...HOME_PAGE,
  continue_watching: [CONTINUE_NO_DURATION],
}

export const EMPTY_HOME: HomePage = { continue_watching: [], behind: [], new_this_week: [] }

/* --- Playback (roadmap M8) -------------------------------------------- */

/**
 * `GET /api/episodes/9001/play`: episode 1 is ready and part-watched, episode
 * 2 is still being prepared, so "next" is offered but not playable. Tests that
 * need the other shapes override `next` / `previous` / `resume_position`.
 */
export const PLAY_INFO: PlayInfo = {
  episode: FRIEREN_DETAIL.episodes[0] as EpisodeOut,
  anime: FRIEREN,
  playlist_url: '/media/9001/index.m3u8',
  duration: 1436.8,
  resume_position: 754,
  previous: null,
  next: { id: 9002, number: 2, state: 'preparing', ready: false },
}

/** The next episode has arrived, so the end overlay can offer it (FR-S5). */
export const PLAY_INFO_NEXT_READY: PlayInfo = {
  ...PLAY_INFO,
  resume_position: null,
  next: { id: 9002, number: 2, state: 'ready', ready: true },
}

/** The last episode of the show: nothing to offer at the end. */
export const PLAY_INFO_LAST: PlayInfo = { ...PLAY_INFO, resume_position: null, next: null }

/**
 * `GET /api/episodes/9002/play`: the episode after `PLAY_INFO_NEXT_READY`,
 * pointing back at 9001. The pair is what a "next, then previous" walk needs.
 */
export const PLAY_INFO_EPISODE_2: PlayInfo = {
  ...PLAY_INFO,
  episode: { ...(FRIEREN_DETAIL.episodes[0] as EpisodeOut), id: 9002, number: 2 },
  playlist_url: '/media/9002/index.m3u8',
  resume_position: null,
  previous: { id: 9001, number: 1, state: 'ready', ready: true },
  next: null,
}

/* --- MyAnimeList link and write log (roadmap M9) ---------------------- */

/** A server with no MAL client id: the page can only explain why it is idle. */
export const MAL_STATUS_UNCONFIGURED: MalStatus = {
  linked: false,
  mal_username: null,
  expires_at: null,
  last_import_at: null,
  needs_relink: false,
  pending_writes: 0,
  failed_writes: 0,
  configured: false,
}

/** Configured, but this viewer has never authorised Arc. */
export const MAL_STATUS_UNLINKED: MalStatus = { ...MAL_STATUS_UNCONFIGURED, configured: true }

/** A working link with two fields queued and one that gave up (FR-M6). */
export const MAL_STATUS_LINKED: MalStatus = {
  linked: true,
  mal_username: 'arcviewer',
  expires_at: '2026-09-08T09:00:00Z',
  last_import_at: '2026-09-07T06:30:00Z',
  needs_relink: false,
  pending_writes: 2,
  failed_writes: 1,
  configured: true,
}

/** The refresh token stopped working: nothing syncs until it is re-authorised. */
export const MAL_STATUS_NEEDS_RELINK: MalStatus = {
  ...MAL_STATUS_LINKED,
  needs_relink: true,
  pending_writes: 0,
  failed_writes: 0,
}

export function malWrite(overrides: Partial<MalWrite> = {}): MalWrite {
  return {
    id: 1,
    anime: FRIEREN,
    field: 'progress',
    old_value: 3,
    new_value: 4,
    cause: 'watch',
    status: 'ok',
    error: null,
    created_at: '2026-09-07T08:00:00Z',
    revertible: true,
    ...overrides,
  }
}

/** MAL's own complaint about the failed row below. */
export const MAL_WRITE_ERROR = 'MAL rejected the update: 400 invalid status'

/** The sentence a skipped row carries: why nothing was sent (FR-M4). */
export const MAL_SKIP_REASON = 'automatic progress never lowers MAL'

/**
 * One row per shape the log has to render: a progress write that landed and
 * can be put back, a status change that failed with MAL's reason, a queued
 * score write that cannot be reverted until it has landed, and a skipped one
 * whose sentence explains why Arc sent nothing at all.
 */
export const MAL_LOG: MalWrite[] = [
  malWrite(),
  malWrite({
    id: 2,
    field: 'status',
    old_value: 'watching',
    new_value: 'on_hold',
    cause: 'manual',
    status: 'failed',
    error: MAL_WRITE_ERROR,
    created_at: '2026-09-07T07:00:00Z',
  }),
  malWrite({
    id: 3,
    anime: FRIEREN_SPECIAL,
    field: 'score',
    old_value: null,
    new_value: 8,
    cause: 'manual',
    status: 'pending',
    created_at: '2026-09-07T06:00:00Z',
    revertible: false,
  }),
  malWrite({
    id: 4,
    anime: FRIEREN_SPECIAL,
    field: 'progress',
    old_value: 9,
    new_value: 2,
    cause: 'watch',
    status: 'skipped',
    error: MAL_SKIP_REASON,
    created_at: '2026-09-07T05:00:00Z',
    revertible: false,
  }),
]

/**
 * Recommendations (spec §4.8, roadmap M12). Two picks, because that is enough
 * to show the grid and one of each interesting shape: a show that is not on
 * the list at all, and one already planned that came from MAL — the two states
 * the status control and the source badge have to render side by side.
 */
export const REC_CASE_FRIEREN =
  'You rated Mushishi a 9 and finished Natsume in a week, so a quiet fantasy that spends its ' +
  'episodes on small human moments is squarely your taste. It is also the only long show here ' +
  'you could start tonight and still be caught up by the weekend.'

export const REC_CASE_SPECIAL =
  'A side story to something you already planned, and short enough to fit an evening. Worth it ' +
  'mainly if the first one lands.'

export const REC_PICKS: RecPick[] = [
  { anime: FRIEREN, case: REC_CASE_FRIEREN },
  { anime: FRIEREN_SPECIAL, case: REC_CASE_SPECIAL },
]

/** A sequel Arc has a row for, offered as a continuation rather than a pick. */
export const FRIEREN_SEASON_2: AnimeSummary = {
  ...FRIEREN,
  id: 189327,
  title: {
    romaji: 'Sousou no Frieren 2nd Season',
    english: null,
    native: '葬送のフリーレン 第2期',
    preferred: 'Sousou no Frieren 2nd Season',
  },
  episodes: null,
  status: 'RELEASING',
  season: 'WINTER',
  season_year: 2026,
  anilist_id: UNLINKED_RELATION.anilist_id,
  mal_id: UNLINKED_RELATION.mal_id,
  source: 'anilist',
  list_status: null,
}

/**
 * Shows following on from the list. Deliberately not one of the picks: the two
 * sections render side by side and each anime must own one status control.
 */
export const REC_CONTINUATIONS: RecContinuation[] = [
  {
    anime: FRIEREN_SEASON_2,
    because: 'Follows Frieren: Beyond Journey’s End, which is on your list.',
  },
]

/**
 * The fallback chain an admin sees: one model good for today, one that has
 * spent its daily quota, and a third reached through a different provider —
 * the three shapes the line has to render.
 */
export const REC_CHAIN: RecModelStatus[] = [
  { provider: 'gemini', model: 'gemini-3.5-flash', available: true },
  { provider: 'gemini', model: 'gemini-2.5-flash', available: false },
  { provider: 'openrouter', model: 'openai/gpt-5-mini', available: true },
]

export const REC_RUN: RecRun = {
  id: 7,
  prompt: 'something short and funny',
  created_at: '2026-09-10T08:00:00Z',
  model: 'claude-opus-5',
  candidate_count: 38,
  picks: REC_PICKS,
  continuations: REC_CONTINUATIONS,
}

/** A run stored before continuations existed: the key is absent, not empty. */
export const REC_RUN_WITHOUT_CONTINUATIONS: RecRun = {
  id: REC_RUN.id,
  prompt: REC_RUN.prompt,
  created_at: REC_RUN.created_at,
  model: REC_RUN.model,
  candidate_count: REC_RUN.candidate_count,
  picks: REC_PICKS,
}

/** A viewer who has run once today and has nine left. No chain: not an admin. */
export const RECS_PAGE: RecsPage = {
  run: REC_RUN,
  remaining_today: 9,
  limit_per_day: 10,
  configured: true,
}

/** The same page as an admin gets it: the fallback chain comes with it. */
export const RECS_PAGE_ADMIN: RecsPage = { ...RECS_PAGE, chain: REC_CHAIN }

/** Nothing run yet: the empty state, with the full day's allowance. */
export const RECS_PAGE_EMPTY: RecsPage = {
  run: null,
  remaining_today: 10,
  limit_per_day: 10,
  configured: true,
}

/** No Anthropic key on the server: there is nothing to offer. */
export const RECS_PAGE_UNCONFIGURED: RecsPage = { ...RECS_PAGE_EMPTY, configured: false }
