import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { AnimeCard } from '@/components/AnimeCard'
import { ErrorState } from '@/components/ErrorState'
import { buttonClass, Chip, EmptyState, Skeleton } from '@/components/ui'
import { catalogErrorMessage, isSearchable, useAnimeSearch, type AnimeSummary } from '@/lib/anime'
import { seasonLabel, useSchedule, type SchedulePage } from '@/lib/schedule'

/**
 * Browse (spec §4.1 FR-C1, §5; design handoff "Browse").
 *
 * The search field lives in the toolbar now, so this page reads `?q=` and
 * renders what it finds there — the URL is still the only source of truth for
 * what is being searched, which is what kept a search shareable and
 * survivable across a refresh. Nothing else about the query changed.
 *
 * With no query there is nothing to search for, and the catalogue has no
 * "everything, newest first" endpoint to offer instead, so the page shows the
 * current season — the same `/api/schedule` answer the Schedule page holds,
 * usually already in cache — and says so rather than claiming to be the whole
 * catalogue.
 */

const IDLE_HINT = 'Search the catalogue by title'

const SEASON_UNAVAILABLE = 'Could not load this season. Search by title instead.'

/** The chip that means "no filter", and the label it wears. */
const ALL = 'All'

/**
 * Every genre present in the results, alphabetically.
 *
 * Built from what came back rather than from a fixed list: a chip that
 * filters the grid to nothing is a dead control, and the catalogue's genre
 * vocabulary is AniList's, not something this client should hardcode.
 */
function genresIn(results: readonly AnimeSummary[]): string[] {
  const seen = new Set<string>()
  for (const anime of results) {
    for (const genre of anime.genres ?? []) {
      if (genre !== '') seen.add(genre)
    }
  }
  return [...seen].sort((a, b) => a.localeCompare(b))
}

/**
 * The season grid flattened into a list of shows, days first and then the
 * films and OVAs with no slot. A show that airs twice a week is one card.
 */
function seasonResults(schedule: SchedulePage): AnimeSummary[] {
  const results: AnimeSummary[] = []
  const seen = new Set<number>()
  for (const entry of [...schedule.days.flatMap((day) => day.entries), ...schedule.unscheduled]) {
    if (seen.has(entry.anime.id)) continue
    seen.add(entry.anime.id)
    results.push(entry.anime)
  }
  return results
}

function Grid({ results, dim }: { results: readonly AnimeSummary[]; dim: boolean }) {
  return (
    <div
      className={`mt-7 grid gap-[26px] [grid-template-columns:repeat(auto-fill,minmax(172px,1fr))] ${
        dim ? 'opacity-60' : ''
      }`}
    >
      {results.map((anime) => (
        <AnimeCard key={anime.id} anime={anime} />
      ))}
    </div>
  )
}

export function Search() {
  const [searchParams] = useSearchParams()
  const query = searchParams.get('q') ?? ''
  const searching = isSearchable(query)

  // Paging belongs to the query, so a new search starts at page one. Adjusted
  // during render rather than in an effect: an effect would let page 3 of the
  // previous search fire one request against the new one first.
  const [page, setPage] = useState(1)
  const [seenQuery, setSeenQuery] = useState(query)
  if (query !== seenQuery) {
    setSeenQuery(query)
    setPage(1)
  }

  const [genre, setGenre] = useState(ALL)

  const search = useAnimeSearch(query, page)
  const schedule = useSchedule()

  const season = schedule.data
  const results = searching
    ? (search.data?.results ?? [])
    : season === undefined
      ? []
      : seasonResults(season)

  const genres = genresIn(results)
  // A filter whose chip is no longer on screen must not keep filtering.
  const active = genre !== ALL && genres.includes(genre) ? genre : ALL
  const shown =
    active === ALL ? results : results.filter((anime) => (anime.genres ?? []).includes(active))

  const lede = searching
    ? `${String(results.length)} result${results.length === 1 ? '' : 's'} for “${query.trim()}”`
    : season === undefined
      ? 'The current season in Arc’s catalogue. Search by title to look further.'
      : `${seasonLabel(season.year, season.season)} in Arc’s catalogue. Search by title to look further.`

  const pending = searching ? search.isPending : schedule.isPending
  const failed = searching ? search.isError : schedule.isError

  return (
    <section>
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        Browse
      </h1>
      <p className="mt-2.5 max-w-[66ch] text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
        {lede}
      </p>

      <div className="mt-6 flex flex-wrap items-center gap-2.5">
        {genres.length === 0 ? null : (
          <div role="group" aria-label="Filter by genre" className="flex flex-wrap gap-2.5">
            <Chip
              active={active === ALL}
              onClick={() => {
                setGenre(ALL)
              }}
            >
              {ALL}
            </Chip>
            {genres.map((value) => (
              <Chip
                key={value}
                active={active === value}
                onClick={() => {
                  setGenre(value)
                }}
              >
                {value}
              </Chip>
            ))}
          </div>
        )}
        <Link to="/recs" className={buttonClass('secondary', 'ml-auto h-11 px-5 text-[15px]')}>
          Recommendations
        </Link>
      </div>

      {failed ? (
        <ErrorState
          className="mt-7"
          message={
            searching
              ? catalogErrorMessage(search.error, 'Search failed. Try again.')
              : catalogErrorMessage(schedule.error, SEASON_UNAVAILABLE)
          }
          pending={searching ? search.isFetching : schedule.isFetching}
          onRetry={() => {
            void (searching ? search.refetch() : schedule.refetch())
          }}
        />
      ) : pending ? (
        <Skeleton shape="key" count={6} className="mt-7" />
      ) : shown.length === 0 ? (
        <EmptyState
          className="mt-7"
          message={
            searching
              ? page === 1
                ? `No results for “${query.trim()}”`
                : 'No more results'
              : IDLE_HINT
          }
        />
      ) : (
        <Grid results={shown} dim={searching && (search.isPlaceholderData || search.isFetching)} />
      )}

      {searching && search.data !== undefined && (search.data.has_next || page > 1) ? (
        <div className="mt-7 flex items-center gap-3">
          <button
            type="button"
            className={buttonClass('chip')}
            disabled={page === 1 || search.isFetching}
            onClick={() => {
              setPage((current) => Math.max(1, current - 1))
            }}
          >
            Previous
          </button>
          <span className="text-[14px] text-[var(--arc-text-muted)] tabular-nums">Page {page}</span>
          <button
            type="button"
            className={buttonClass('chip')}
            disabled={!search.data.has_next || search.isFetching}
            onClick={() => {
              setPage((current) => current + 1)
            }}
          >
            More
          </button>
        </div>
      ) : null}
    </section>
  )
}
