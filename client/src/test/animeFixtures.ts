import type {
  AnimeDetail,
  AnimeRelation,
  AnimeSearchResponse,
  AnimeSummary,
  ListEntry,
} from '@/lib/anime'

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

/**
 * One episode in each of the three shapes the show page has to render: ready
 * (playable), mid-pipeline, and unaired / not wanted.
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
