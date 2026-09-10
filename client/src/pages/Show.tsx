import { Link, useParams } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ErrorState } from '@/components/ErrorState'
import { ListStatusControl } from '@/components/ListStatusControl'
import {
  anilistUrl,
  catalogErrorMessage,
  episodeDetailLine,
  episodeProblem,
  episodeProgressPercent,
  episodeStateClass,
  episodeStateLabel,
  formatAirDate,
  listErrorMessage,
  malUrl,
  useAnime,
  useRetryTranscode,
  useSetListEntry,
  type AnimeDetail,
  type AnimeRelation,
  type EpisodeOut,
} from '@/lib/anime'
import { isStatus, useMe } from '@/lib/auth'
import type { MalSync } from '@/lib/mal'
import { useMarkWatched, useUnmarkWatched } from '@/lib/playback'

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

/**
 * Where this show stands with MyAnimeList (spec §4.7 FR-M6). It sits beside
 * the controls that cause the writes, because that is where the question comes
 * up — "did that stick?" — and it says nothing at all when there is no link,
 * so an unlinked account never carries a badge it cannot act on.
 *
 * A failure is the only one that is a problem, so it is the only one that
 * names the error and offers the sync log, where the write can be retried or
 * put back.
 */
function MalSyncIndicator({ sync }: { sync: MalSync | undefined }) {
  if (sync === undefined || sync.state === 'unlinked') return null

  if (sync.state === 'failed') {
    const reason = sync.error === null || sync.error === '' ? null : sync.error

    return (
      <p role="alert" className="py-1.5 text-sm text-[var(--arc-warn)]">
        {reason === null ? 'MAL: failed' : `MAL: failed — ${reason}`}{' '}
        <Link to="/mal" className="text-[var(--arc-accent)] hover:underline">
          Sync log
        </Link>
      </p>
    )
  }

  const pending = sync.state === 'pending'

  return (
    <p
      className={`py-1.5 text-sm ${pending ? 'text-[var(--arc-accent)]' : 'text-[var(--arc-text-muted)]'}`}
      title={sync.last_write_at === null ? undefined : `Last write ${sync.last_write_at}`}
    >
      {pending ? 'MAL: pending' : 'MAL: synced'}
    </p>
  )
}

/** What the bar is measuring, when it is not the transfer itself. */
const PREPARING_LABEL = 'Preparing'
const ACQUISITION_LABEL = 'Acquisition progress'

/** Why a transcode retry did not get as far as being queued. */
const RETRY_FAILED = 'Could not queue a retry.'

/** Why a manual watched write did not stick (FR-W3). */
const WATCHED_FAILED = 'Could not save that.'

/**
 * How far the work on an episode has got (FR-A7, FR-P4). The bar carries the
 * number for assistive tech and the text beside it carries the same number for
 * everyone else, so neither has to read the other's markup. Downloading and
 * preparing share it — same shape, different label, because a screen reader
 * has no badge beside it to say which job is running.
 */
function AcquisitionProgress({ percent, label }: { percent: number; label: string }) {
  const text = `${String(percent)}%`

  return (
    <span className="mt-1 flex items-center gap-1.5">
      <span
        role="progressbar"
        aria-label={label}
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={text}
        className="block h-1 w-16 overflow-hidden rounded-full bg-[var(--arc-border)]"
      >
        <span
          className="block h-full rounded-full bg-[var(--arc-accent)]"
          style={{ width: text }}
        />
      </span>
      <span className="text-xs text-[var(--arc-text-muted)] tabular-nums">{text}</span>
    </span>
  )
}

/**
 * Why an episode is not coming (FR-A6) or why its transcode broke (FR-P4).
 * "Unavailable" or "Failed" on its own invites the question, so the marker
 * answers it on hover and says the whole thing to a screen reader, which
 * cannot hover.
 */
function ProblemHint({ label, reason }: { label: string; reason: string }) {
  return (
    <span
      role="img"
      title={reason}
      aria-label={`${label}: ${reason}`}
      className="ml-1.5 cursor-help text-xs text-[var(--arc-text-muted)]"
    >
      ⓘ
    </span>
  )
}

/**
 * Send a failed episode back to the transcoder (FR-P4). Admin-only, so the
 * button is rendered only for one — a viewer who cannot retry is better off
 * not seeing an action that would 403.
 */
function RetryTranscode({ animeId, episodeId }: { animeId: number; episodeId: number }) {
  const retry = useRetryTranscode()

  return (
    <>
      <button
        type="button"
        disabled={retry.isPending}
        onClick={() => {
          retry.mutate({ animeId, episodeId })
        }}
        className="mt-1 rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface-raised)] px-2 py-0.5 text-xs text-[var(--arc-text)] hover:border-[var(--arc-accent)] disabled:opacity-60"
      >
        Retry
      </button>
      {retry.isError ? (
        <span role="alert" className="mt-1 text-xs text-[var(--arc-error)]">
          {RETRY_FAILED}
        </span>
      ) : null}
    </>
  )
}

/**
 * Marking an episode watched by hand (FR-W3), and taking the mark off again.
 *
 * Offered for anything that has aired, whatever state Arc has the file in: the
 * whole point of the manual mark is the episode watched somewhere else, which
 * is exactly the case where Arc has no file. Unaired episodes get nothing,
 * since there is nothing yet to have watched.
 */
function WatchedControl({ animeId, episode }: { animeId: number; episode: EpisodeOut }) {
  const mark = useMarkWatched()
  const unmark = useUnmarkWatched()
  const pending = mark.isPending || unmark.isPending
  const failed = mark.isError || unmark.isError

  if (!episode.watched && !episode.aired) return null

  const buttonClass =
    'text-xs text-[var(--arc-text-muted)] underline-offset-2 hover:text-[var(--arc-text)] hover:underline disabled:opacity-60'

  return (
    <span className="flex flex-col items-center gap-0.5">
      {episode.watched ? (
        <>
          <span className="text-[var(--arc-ok)]" role="img" aria-label="Watched">
            ✓
          </span>
          <button
            type="button"
            disabled={pending}
            onClick={() => {
              unmark.mutate({ episodeId: episode.id, animeId })
            }}
            className={buttonClass}
          >
            Unmark
          </button>
        </>
      ) : (
        <button
          type="button"
          disabled={pending}
          onClick={() => {
            mark.mutate({ episodeId: episode.id, animeId })
          }}
          className={buttonClass}
        >
          Mark watched
        </button>
      )}
      {failed ? (
        <span role="alert" className="text-xs text-[var(--arc-error)]">
          {WATCHED_FAILED}
        </span>
      ) : null}
    </span>
  )
}

function EpisodeRow({
  animeId,
  episode,
  isAdmin,
  timezone,
}: {
  animeId: number
  episode: EpisodeOut
  isAdmin: boolean
  timezone?: string
}) {
  const title = episode.title ?? `Episode ${episode.number}`
  const percent = episodeProgressPercent(episode)
  const problem = episodeProblem(episode)
  const detail = episodeDetailLine(episode)

  return (
    <tr className="border-t border-[var(--arc-border)]">
      <td className="px-3 py-2 text-[var(--arc-text-muted)] tabular-nums">{episode.number}</td>
      <td className="px-3 py-2 text-[var(--arc-text)]">
        {title}
        {detail === null ? null : (
          <span className="mt-0.5 block text-xs text-[var(--arc-text-muted)]">{detail}</span>
        )}
      </td>
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
        <span className="flex flex-col items-start">
          <span className="whitespace-nowrap">
            <span
              className={`inline-block rounded-full border px-2 py-0.5 text-xs ${episodeStateClass(episode.state)}`}
            >
              {episodeStateLabel(episode.state)}
            </span>
            {problem === null ? null : (
              <ProblemHint label={problem.label} reason={problem.reason} />
            )}
          </span>
          {percent === null ? null : (
            <AcquisitionProgress
              percent={percent}
              label={episode.state === 'preparing' ? PREPARING_LABEL : ACQUISITION_LABEL}
            />
          )}
          {episode.state === 'failed' && isAdmin ? (
            <RetryTranscode animeId={animeId} episodeId={episode.id} />
          ) : null}
        </span>
      </td>
      <td className="px-3 py-2 text-center">
        <WatchedControl animeId={animeId} episode={episode} />
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

function Episodes({
  animeId,
  episodes,
  isAdmin,
  timezone,
}: {
  animeId: number
  episodes: EpisodeOut[]
  isAdmin: boolean
  timezone?: string
}) {
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
            <EpisodeRow
              key={episode.id}
              animeId={animeId}
              episode={episode}
              isAdmin={isAdmin}
              timezone={timezone}
            />
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
  const { data: anime, isPending, isError, isFetching, error, refetch } = useAnime(animeId)

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
      <ErrorState
        message={catalogErrorMessage(error, 'Could not load this show.')}
        pending={isFetching}
        onRetry={() => {
          void refetch()
        }}
      />
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
                <MalSyncIndicator sync={entry.mal_sync} />
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
      <Episodes
        animeId={anime.id}
        episodes={anime.episodes}
        isAdmin={me?.role === 'admin'}
        timezone={timezone}
      />

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
