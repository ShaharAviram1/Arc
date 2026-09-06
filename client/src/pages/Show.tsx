import { Link, useParams } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ListStatusControl } from '@/components/ListStatusControl'
import {
  anilistUrl,
  catalogErrorMessage,
  episodeStateClass,
  episodeStateLabel,
  formatAirDate,
  listErrorMessage,
  malUrl,
  useAnime,
  useSetListEntry,
  type AnimeDetail,
  type AnimeRelation,
  type EpisodeOut,
} from '@/lib/anime'
import { isStatus, useMe } from '@/lib/auth'

const SCORES = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]

const selectClass =
  'rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-2 py-1.5 text-sm text-[var(--arc-text)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)] disabled:opacity-60'

/** Why a date carries "est." — the tooltip is the whole explanation (FR-C6). */
const ESTIMATED_HINT = 'Estimated from the broadcast slot'

const MAL_FALLBACK_NOTICE =
  'Catalogue data via MyAnimeList — AniList is unavailable. Air dates are estimated.'

/**
 * Why a related title is not a link: Arc has no row for it, so there is no
 * page to go to. Deliberately not a link to AniList or MAL either — the
 * external links row already covers this show, and a related title is not it.
 */
const UNLINKED_RELATION_HINT = 'Not in the catalogue yet'

/** "RELEASING" → "Releasing"; AniList shouts its enums. */
function humanize(value: string): string {
  return value.charAt(0) + value.slice(1).toLowerCase().replace(/_/g, ' ')
}

function metaLine(anime: AnimeDetail): string {
  const parts: string[] = []
  if (anime.format !== null) parts.push(anime.format)
  if (anime.episode_count !== null) parts.push(`${anime.episode_count} episodes`)
  // AniList does not always know a show's airing status; skip rather than guess.
  if (anime.status !== null) parts.push(humanize(anime.status))
  if (anime.season_year !== null) {
    parts.push(
      anime.season === null
        ? String(anime.season_year)
        : `${humanize(anime.season)} ${anime.season_year}`,
    )
  }
  if (anime.studio !== null) parts.push(anime.studio)
  return parts.join(' · ')
}

/** Romaji / native, shown only when they add something to the preferred title. */
function altTitles(anime: AnimeDetail): string {
  const { preferred, romaji, native } = anime.title
  const alternatives = [romaji, native].filter(
    (value): value is string => value !== null && value !== '' && value !== preferred,
  )
  return [...new Set(alternatives)].join(' · ')
}

/**
 * Out to whichever catalogues know this show. Arc's own id is internal
 * (FR-C6), so these are the only links a person can share or cross-check with;
 * a source that has not filled the record yet simply has no link.
 */
function ExternalLinks({ anime }: { anime: AnimeDetail }) {
  const links: { label: string; href: string }[] = []
  if (anime.anilist_id !== null)
    links.push({ label: 'AniList', href: anilistUrl(anime.anilist_id) })
  if (anime.mal_id !== null) links.push({ label: 'MAL', href: malUrl(anime.mal_id) })
  if (links.length === 0) return null

  return (
    <ul className="mt-2 flex flex-wrap gap-x-3 text-xs">
      {links.map(({ label, href }) => (
        <li key={label}>
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className="text-[var(--arc-accent)] hover:underline"
          >
            {label}
          </a>
        </li>
      ))}
    </ul>
  )
}

function ScoreControl({ animeId, score }: { animeId: number; score: number | null }) {
  const setEntry = useSetListEntry()

  return (
    <div>
      <select
        aria-label="Score"
        value={score === null ? '' : String(score)}
        disabled={setEntry.isPending}
        onChange={(event) => {
          const raw = event.target.value
          setEntry.mutate({ animeId, score: raw === '' ? null : Number(raw) })
        }}
        className={selectClass}
      >
        <option value="">—</option>
        {SCORES.map((value) => (
          <option key={value} value={value}>
            {value}
          </option>
        ))}
      </select>
      {setEntry.isError ? (
        <p role="alert" className="mt-1 text-xs text-[var(--arc-error)]">
          {listErrorMessage(setEntry.error)}
        </p>
      ) : null}
    </div>
  )
}

function EpisodeRow({ episode, timezone }: { episode: EpisodeOut; timezone?: string }) {
  const title = episode.title ?? `Episode ${episode.number}`

  return (
    <tr className="border-t border-[var(--arc-border)]">
      <td className="px-3 py-2 text-[var(--arc-text-muted)] tabular-nums">{episode.number}</td>
      <td className="px-3 py-2 text-[var(--arc-text)]">{title}</td>
      <td className="px-3 py-2 whitespace-nowrap text-[var(--arc-text-muted)]">
        {formatAirDate(episode.air_at, timezone)}
        {episode.air_at_estimated ? (
          <span
            title={ESTIMATED_HINT}
            className="ml-1.5 text-xs text-[var(--arc-text-muted)] italic"
          >
            est.
          </span>
        ) : null}
      </td>
      <td className="px-3 py-2 text-[var(--arc-text-muted)]">
        {episode.aired ? 'Aired' : 'Unaired'}
      </td>
      <td className="px-3 py-2">
        <span
          className={`inline-block rounded-full border px-2 py-0.5 text-xs ${episodeStateClass(episode.state)}`}
        >
          {episodeStateLabel(episode.state)}
        </span>
      </td>
      <td className="px-3 py-2 text-center">
        {episode.watched ? (
          <span className="text-[var(--arc-ok)]" role="img" aria-label="Watched">
            ✓
          </span>
        ) : null}
      </td>
      <td className="px-3 py-2 text-right">
        {episode.state === 'ready' ? (
          <Link
            to={`/watch/${episode.id}`}
            className="rounded-md bg-[var(--arc-accent)] px-2.5 py-1 text-xs font-medium text-[var(--arc-accent-contrast)] hover:opacity-90"
          >
            Play
          </Link>
        ) : (
          <span className="text-xs text-[var(--arc-text-muted)]">
            {episodeStateLabel(episode.state)}
          </span>
        )}
      </td>
    </tr>
  )
}

function Episodes({ episodes, timezone }: { episodes: EpisodeOut[]; timezone?: string }) {
  if (episodes.length === 0) {
    return <p className="mt-3 text-sm text-[var(--arc-text-muted)]">No episodes known yet.</p>
  }

  return (
    <div className="mt-3 overflow-x-auto rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)]">
      <table className="w-full min-w-[40rem] text-left text-sm">
        <thead className="text-xs tracking-wide text-[var(--arc-text-muted)] uppercase">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              #
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Title
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Air date
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Aired
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              State
            </th>
            <th scope="col" className="px-3 py-2 text-center font-medium">
              Watched
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Play
            </th>
          </tr>
        </thead>
        <tbody>
          {episodes.map((episode) => (
            <EpisodeRow key={episode.id} episode={episode} timezone={timezone} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The server drops a relation that names neither catalogue, so at least one of
 * the two external ids is always set and the pair identifies the row — which
 * Arc's own id cannot do here, being null for anything not in the catalogue.
 */
function relationKey(relation: AnimeRelation): string {
  return `${relation.relation_type}:${relation.anilist_id ?? '-'}:${relation.mal_id ?? '-'}`
}

/**
 * A related show, linked when Arc has a row for it and plain text when it does
 * not (FR-C6): `id` is null until something has pulled that title in.
 */
function RelationItem({ relation }: { relation: AnimeRelation }) {
  return (
    <li>
      {relation.id === null ? (
        <span title={UNLINKED_RELATION_HINT} className="text-[var(--arc-text-muted)]">
          {relation.title.preferred}
        </span>
      ) : (
        <Link to={`/anime/${relation.id}`} className="text-[var(--arc-accent)] hover:underline">
          {relation.title.preferred}
        </Link>
      )}
      <span className="ml-2 text-xs text-[var(--arc-text-muted)]">
        {humanize(relation.relation_type)}
      </span>
    </li>
  )
}

/**
 * Show page (spec §5, §4.6 FR-W2, roadmap M3): catalogue detail, the viewer's
 * list controls, and every episode with the state Arc has it in (spec §6).
 */
export function Show() {
  const { id } = useParams()
  const animeId = Number(id)
  const { data: me } = useMe()
  const { data: anime, isPending, isError, error } = useAnime(animeId)

  if (!Number.isInteger(animeId) || animeId <= 0) {
    return <NotFoundState />
  }

  if (isPending) {
    return (
      <p role="status" className="text-sm text-[var(--arc-text-muted)]">
        Loading…
      </p>
    )
  }

  if (isError) {
    if (isStatus(error, 404)) return <NotFoundState />
    return (
      <p role="alert" className="text-sm text-[var(--arc-error)]">
        {catalogErrorMessage(error, 'Could not load this show.')}
      </p>
    )
  }

  const timezone = me?.timezone
  const alternatives = altTitles(anime)
  const entry = anime.list_entry

  return (
    <section className="mx-auto max-w-5xl">
      {anime.banner_url === null ? null : (
        <img
          src={anime.banner_url}
          alt=""
          className="mb-6 h-40 w-full rounded-lg border border-[var(--arc-border)] object-cover"
        />
      )}

      <header className="flex flex-col gap-6 sm:flex-row">
        <CoverThumb url={anime.cover_url} className="aspect-[2/3] w-40 rounded-lg" />

        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
            {anime.title.preferred}
          </h1>
          {alternatives === '' ? null : (
            <p className="mt-1 text-sm text-[var(--arc-text-muted)]">{alternatives}</p>
          )}
          <p className="mt-2 text-sm text-[var(--arc-text-muted)]">{metaLine(anime)}</p>

          {anime.source === 'mal' ? (
            <p className="mt-2 rounded-md border border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10 px-2 py-1 text-xs text-[var(--arc-warn)]">
              {MAL_FALLBACK_NOTICE}
            </p>
          ) : null}

          <ExternalLinks anime={anime} />

          {anime.genres.length === 0 ? null : (
            <ul className="mt-3 flex flex-wrap gap-2">
              {anime.genres.map((genre) => (
                <li
                  key={genre}
                  className="rounded-full border border-[var(--arc-border)] bg-[var(--arc-surface)] px-2 py-0.5 text-xs text-[var(--arc-text-muted)]"
                >
                  {genre}
                </li>
              ))}
            </ul>
          )}

          <div className="mt-4 flex flex-wrap items-start gap-4">
            <ListStatusControl animeId={anime.id} status={anime.list_status} className="w-44" />
            {entry === null ? null : (
              <>
                <ScoreControl animeId={anime.id} score={entry.score} />
                <p className="py-1.5 text-sm text-[var(--arc-text-muted)]">
                  Watched {entry.progress} / {anime.episode_count ?? '?'}
                </p>
              </>
            )}
          </div>

          {anime.next_airing === null ? null : (
            <p className="mt-3 text-sm text-[var(--arc-text-muted)]">
              Next episode: {anime.next_airing.episode} airs{' '}
              {formatAirDate(anime.next_airing.at, timezone)}
            </p>
          )}
        </div>
      </header>

      {anime.synopsis === null || anime.synopsis === '' ? null : (
        <p className="mt-6 text-sm leading-relaxed whitespace-pre-line text-[var(--arc-text-muted)]">
          {anime.synopsis}
        </p>
      )}

      <h2 className="mt-8 text-lg font-semibold tracking-tight text-[var(--arc-text)]">Episodes</h2>
      <Episodes episodes={anime.episodes} timezone={timezone} />

      {anime.relations.length === 0 ? null : (
        <>
          <h2 className="mt-8 text-lg font-semibold tracking-tight text-[var(--arc-text)]">
            Related
          </h2>
          <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-2 text-sm">
            {anime.relations.map((relation) => (
              <RelationItem key={relationKey(relation)} relation={relation} />
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

function NotFoundState() {
  return (
    <section className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
        Show not found
      </h1>
      <p className="mt-2 text-sm text-[var(--arc-text-muted)]">Arc has no anime with that id.</p>
      <Link
        className="mt-6 inline-block text-sm text-[var(--arc-accent)] hover:underline"
        to="/search"
      >
        Back to search
      </Link>
    </section>
  )
}
