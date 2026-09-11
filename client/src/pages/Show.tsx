import { Link, useParams } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import { ListStatusControl } from '@/components/ListStatusControl'
import {
  Artwork,
  Button,
  buttonClass,
  cx,
  EmptyState,
  FOCUS_RING,
  HeroFrame,
  PlayGlyph,
  Shelf,
  Skeleton,
} from '@/components/ui'
import {
  anilistUrl,
  bannerArt,
  catalogErrorMessage,
  episodeProgressPercent,
  episodeProblem,
  episodeStateLabel,
  formatAirDate,
  keyVisual,
  listErrorMessage,
  malUrl,
  useAnime,
  useRetryTranscode,
  useSetListEntry,
  type AnimeCredit,
  type AnimeDetail,
  type AnimeRelation,
  type EpisodeOut,
} from '@/lib/anime'
import { isStatus, useMe } from '@/lib/auth'
import type { MalSync } from '@/lib/mal'
import { useMarkWatched, useUnmarkWatched } from '@/lib/playback'

/**
 * The show page (spec §5, §4.6 FR-W2, roadmap M3, restyled for M15).
 *
 * The design's grammar: a framed 21:9 hero with the title in it (`HeroFrame`
 * decides between the banner and the poster wash), one white primary action
 * under it, and then the episodes as roomy grouped rows rather than a table.
 * Nothing about what the page *does* moved — the same
 * queries, the same list mutations, the same per-episode controls — only where
 * the controls sit and what they look like.
 */

const SCORES = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]

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

const NO_EPISODES = 'No episodes known yet. Arc fills them in as the catalogue answers.'

/** What the primary action says when there is nothing playable to offer. */
const NOTHING_READY = 'Nothing ready yet'

const FRANCHISE_LEDE =
  'Prequels first, then this show, then what follows. Side stories sit outside the order.'

/**
 * A glass pill that happens to be a native `<select>`.
 *
 * The design draws the list status and the score as two 48px glass pills with
 * a chevron. They stay `<select>`s underneath — the control, its mutations and
 * its accessible name are exactly what they were before this pass, and a
 * hand-built menu would be a new keyboard implementation for no gain. The
 * `!` utilities win over the classes `ListStatusControl` sets on its own
 * element; the chevron is the wrapper's, since a native select cannot be given
 * one.
 */
const PILL_WRAP = cx(
  'relative inline-flex flex-col items-start',
  "after:pointer-events-none after:absolute after:top-6 after:right-[18px] after:-translate-y-1/2 after:text-[13px] after:leading-none after:text-[var(--arc-text-muted)] after:content-['⌄']",
  '[&_option]:bg-[var(--arc-bg)] [&_option]:text-[var(--arc-text)]',
)

/** The pill itself, for the one select this page owns outright. */
const PILL_SELECT = cx(
  'h-12 cursor-pointer appearance-none rounded-full border-[0.5px] border-[var(--arc-border-strong)]',
  'bg-[var(--arc-surface-raised)] pr-[44px] pl-[22px] text-[16px] text-[var(--arc-text)]',
  'disabled:cursor-not-allowed disabled:opacity-60',
  FOCUS_RING,
)

/** The same clothes, forced onto a select this page does not render itself. */
const PILL_WRAP_FORCED = cx(
  PILL_WRAP,
  '[&_select]:h-12! [&_select]:w-auto! [&_select]:cursor-pointer [&_select]:appearance-none',
  '[&_select]:rounded-full! [&_select]:border-[0.5px]! [&_select]:border-[var(--arc-border-strong)]!',
  '[&_select]:bg-[var(--arc-surface-raised)]! [&_select]:py-0! [&_select]:pr-[44px]! [&_select]:pl-[22px]!',
  '[&_select]:text-[16px]! [&_select]:text-[var(--arc-text)]!',
  '[&_select]:outline-offset-2! [&_select]:outline-[var(--arc-focus)]!',
)

/** "RELEASING" → "Releasing"; AniList shouts its enums. */
function humanize(value: string): string {
  return value.charAt(0) + value.slice(1).toLowerCase().replace(/_/g, ' ')
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
 * An episode still, or nothing at all.
 *
 * Deliberately no fallback to the show's own art: the same banner cropped
 * fourteen times down a list of episodes reads as fourteen identical pictures
 * of nothing, where the stripe placeholder reads as "no still for this one"
 * and lets the row's words carry it.
 */
function stillArt(episode: EpisodeOut): string | null {
  return episode.still_url ?? null
}

function watchedCount(episodes: EpisodeOut[]): number {
  return episodes.filter((episode) => episode.watched).length
}

/**
 * "Madhouse · Fall 2023 · 28 episodes · 8 watched".
 *
 * Studio first, because the design treats it as the auteur credit. The
 * language clause the design shows ("subtitled, Japanese audio") is left off:
 * nothing in `/api/auth/me` exposes the server's language preference to the
 * client, and each episode's row already names what its own file actually is.
 */
function metaLine(anime: AnimeDetail): string {
  const parts: string[] = []
  if (anime.studio !== null) parts.push(anime.studio)
  if (anime.season_year !== null) {
    parts.push(
      anime.season === null
        ? String(anime.season_year)
        : `${humanize(anime.season)} ${String(anime.season_year)}`,
    )
  }
  if (anime.episode_count !== null) parts.push(`${String(anime.episode_count)} episodes`)
  const watched = watchedCount(anime.episodes)
  if (watched > 0) parts.push(`${String(watched)} watched`)
  return parts.join(' · ')
}

/**
 * The episode the primary action offers: the first one that has arrived and
 * has not been watched, else the first that has arrived at all — a show
 * already finished still has something to play. Null when nothing is ready,
 * which is what turns the button into the disabled notice.
 */
function playableEpisode(episodes: EpisodeOut[]): EpisodeOut | null {
  const ready = episodes.filter((episode) => episode.state === 'ready')
  return ready.find((episode) => !episode.watched) ?? ready[0] ?? null
}

/** Whether an upcoming episode lands today in the viewer's own zone (FR-C3). */
function airsToday(iso: string | null, tz?: string): boolean {
  if (iso === null || iso === '') return false
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return false
  const options: Intl.DateTimeFormatOptions = { year: 'numeric', month: '2-digit', day: '2-digit' }
  try {
    const format = new Intl.DateTimeFormat('en-CA', {
      ...options,
      ...(tz === undefined || tz === '' ? {} : { timeZone: tz }),
    })
    return format.format(at) === format.format(new Date())
  } catch {
    const format = new Intl.DateTimeFormat('en-CA', options)
    return format.format(at) === format.format(new Date())
  }
}

/**
 * Arc's own word for an episode it has not been asked to fetch is
 * "not wanted", which reads as a refusal rather than as the resting state it
 * is. The rest of the lifecycle (spec §6) keeps its label.
 */
function restingLabel(state: string): string {
  return state === 'not_wanted' ? 'Not fetched' : episodeStateLabel(state)
}

type StateTone = 'muted' | 'bright' | 'ember' | 'error'

const TONE_CLASS: Record<StateTone, string> = {
  muted: 'text-[var(--arc-text-muted)]',
  bright: 'text-[var(--arc-text)]',
  ember: 'text-[var(--arc-ember)]',
  error: 'text-[var(--arc-error)]',
}

interface EpisodeState {
  label: string
  tone: StateTone
  /** 0–100 while a transfer or a transcode is running; null otherwise (FR-A7). */
  percent: number | null
  /** What the percentage is measuring, for a screen reader with no badge to read. */
  progressLabel: string
}

/**
 * The one line in the state column, and the colour it is said in. The design
 * names six of them; the rest of Arc's lifecycle (spec §6) falls through to
 * its own label in the muted tone, which is what "a resting state is never
 * error-coloured" means in practice.
 */
function episodeState(episode: EpisodeOut, tz?: string): EpisodeState {
  const quiet = { tone: 'muted' as StateTone, percent: null, progressLabel: '' }

  if (episode.state === 'failed' || episode.state === 'unavailable') {
    return {
      label: episodeStateLabel(episode.state),
      tone: 'error',
      percent: null,
      progressLabel: '',
    }
  }
  // Work in flight outranks everything else the row could say, watched
  // included: it is the only part of the line that is still changing.
  const percent = episodeProgressPercent(episode)
  if (percent !== null) {
    return {
      label: `${episodeStateLabel(episode.state)} ${String(percent)}%`,
      tone: 'muted',
      percent,
      progressLabel: episode.state === 'preparing' ? 'Preparing' : 'Acquisition progress',
    }
  }
  if (episode.watched) return { label: 'Watched', ...quiet }
  if (!episode.aired) {
    return airsToday(episode.air_at, tz)
      ? { label: 'Airs tonight', tone: 'ember', percent: null, progressLabel: '' }
      : { label: 'Not yet aired', ...quiet }
  }
  if (episode.state === 'ready') {
    return { label: 'Ready to play', tone: 'bright', percent: null, progressLabel: '' }
  }
  return { label: restingLabel(episode.state), ...quiet }
}

/**
 * "Fri, 30 Aug 2023 · 24 min · 1080p · SubsPlease" — when it aired, how long
 * it runs, and which release Arc actually has. The fansub group is part of the
 * information, not noise (FR-A3, FR-P4); anything the catalogue or the parser
 * did not fill is dropped rather than shown empty.
 */
function episodeMeta(episode: EpisodeOut, tz?: string): string {
  const parts = [formatAirDate(episode.air_at, tz)]
  const { rendition, release } = episode
  if (rendition !== null && rendition.duration > 0) {
    parts.push(`${String(Math.round(rendition.duration / 60))} min`)
  }
  const resolution =
    rendition !== null && rendition.height > 0
      ? `${String(rendition.height)}p`
      : release?.resolution
  if (resolution !== null && resolution !== undefined && resolution !== '') parts.push(resolution)
  if (release !== null && release.group !== null && release.group !== '') parts.push(release.group)
  return parts.join(' · ')
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
    <p className="mt-2.5 flex flex-wrap gap-x-4 text-[13px] text-[var(--arc-text-muted)]">
      {links.map(({ label, href }) => (
        <a
          key={label}
          href={href}
          target="_blank"
          rel="noreferrer"
          className={cx(
            'underline-offset-4 hover:text-[var(--arc-text)] hover:underline',
            FOCUS_RING,
          )}
        >
          {label}
        </a>
      ))}
    </p>
  )
}

function ScoreControl({ animeId, score }: { animeId: number; score: number | null }) {
  const setEntry = useSetListEntry()

  return (
    <div className={PILL_WRAP}>
      <select
        aria-label="Score"
        value={score === null ? '' : String(score)}
        disabled={setEntry.isPending}
        onChange={(event) => {
          const raw = event.target.value
          setEntry.mutate({ animeId, score: raw === '' ? null : Number(raw) })
        }}
        className={PILL_SELECT}
      >
        <option value="">Not rated</option>
        {SCORES.map((value) => (
          <option key={value} value={value}>
            {`Rated ${String(value)}`}
          </option>
        ))}
      </select>
      {setEntry.isError ? (
        <p role="alert" className="mt-1.5 text-[13px] text-[var(--arc-error)]">
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
      <p role="alert" className="text-[14px] text-[var(--arc-error)]">
        {reason === null ? 'MAL: failed' : `MAL: failed — ${reason}`}{' '}
        <Link to="/mal" className="underline underline-offset-4">
          Sync log
        </Link>
      </p>
    )
  }

  return (
    <p
      className="text-[14px] text-[var(--arc-text-muted)]"
      title={sync.last_write_at === null ? undefined : `Last write ${sync.last_write_at}`}
    >
      {sync.state === 'pending' ? 'MAL: pending' : 'MAL: synced'}
    </p>
  )
}

/** Why a transcode retry did not get as far as being queued. */
const RETRY_FAILED = 'Could not queue a retry.'

/** Why a manual watched write did not stick (FR-W3). */
const WATCHED_FAILED = 'Could not save that.'

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
      className="ml-1.5 cursor-help text-[13px] text-[var(--arc-text-muted)]"
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
        className={buttonClass('chip', 'px-4 text-[13px]')}
      >
        Retry
      </button>
      {retry.isError ? (
        <span role="alert" className="text-[13px] text-[var(--arc-error)]">
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

  return (
    <span className="flex flex-col items-end gap-1.5">
      <button
        type="button"
        disabled={pending}
        onClick={() => {
          const input = { episodeId: episode.id, animeId }
          if (episode.watched) unmark.mutate(input)
          else mark.mutate(input)
        }}
        className={buttonClass('chip', 'px-4 text-[13px]')}
      >
        {episode.watched ? 'Unmark' : 'Mark watched'}
      </button>
      {failed ? (
        <span role="alert" className="text-[13px] text-[var(--arc-error)]">
          {WATCHED_FAILED}
        </span>
      ) : null}
    </span>
  )
}

/**
 * One episode (spec §6).
 *
 * The whole row is the target when the episode is playable — an overlay on the
 * title link rather than a row-wide `<Link>`, because the row also holds a
 * button and an anchor may not contain one. A row with nothing to play is not
 * a control at all: no hover fill, no focus stop, nothing to click.
 */
function EpisodeRow({
  anime,
  episode,
  isAdmin,
  timezone,
}: {
  anime: AnimeDetail
  episode: EpisodeOut
  isAdmin: boolean
  timezone?: string
}) {
  const playable = episode.state === 'ready'
  const title = episode.title ?? `Episode ${String(episode.number)}`
  const state = episodeState(episode, timezone)
  const problem = episodeProblem(episode)

  return (
    <div
      className={cx(
        'relative flex w-full items-center gap-5 rounded-row p-3.5 transition-colors duration-200',
        playable ? 'hover:bg-[var(--arc-surface-hover)]' : '',
      )}
    >
      <Artwork
        url={stillArt(episode)}
        shape="still"
        radius="still"
        className="w-[152px] shrink-0"
      />

      <div className="min-w-0 flex-1">
        <p className="text-[16px] font-medium text-[var(--arc-text)]">
          {playable ? (
            <Link
              to={`/watch/${String(episode.id)}`}
              className={cx('after:absolute after:inset-0 after:rounded-row', FOCUS_RING)}
            >
              {`${String(episode.number)}. ${title}`}
            </Link>
          ) : (
            `${String(episode.number)}. ${title}`
          )}
        </p>
        <p className="mt-1.5 text-[13px] text-[var(--arc-text-muted)]">
          {episodeMeta(episode, timezone)}
          {episode.air_at_estimated ? (
            <span title={ESTIMATED_HINT} className="ml-1.5 italic">
              est.
            </span>
          ) : null}
        </p>
      </div>

      <div className="flex w-[132px] shrink-0 flex-col items-end gap-1.5 text-right text-[14px]">
        <span className={TONE_CLASS[state.tone]}>
          <span>{state.label}</span>
          {problem === null ? null : <ProblemHint label={problem.label} reason={problem.reason} />}
        </span>
        {state.percent === null ? null : (
          <span
            role="progressbar"
            aria-label={state.progressLabel}
            aria-valuenow={state.percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuetext={`${String(state.percent)}%`}
            className="block h-[2px] w-11 overflow-hidden rounded-full bg-[rgba(255,255,255,0.16)]"
          >
            <span
              className="block h-full bg-[var(--arc-text-muted)]"
              style={{ width: `${String(state.percent)}%` }}
            />
          </span>
        )}
        {episode.state === 'failed' && isAdmin ? (
          <span className="relative z-10 flex flex-col items-end gap-1.5">
            <RetryTranscode animeId={anime.id} episodeId={episode.id} />
          </span>
        ) : null}
      </div>

      <div className="relative z-10 shrink-0">
        <WatchedControl animeId={anime.id} episode={episode} />
      </div>
    </div>
  )
}

function Episodes({
  anime,
  isAdmin,
  timezone,
}: {
  anime: AnimeDetail
  isAdmin: boolean
  timezone?: string
}) {
  return (
    <section className="mt-14">
      <h2 className="text-[24px] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
        Episodes
      </h2>
      {anime.episodes.length === 0 ? (
        <EmptyState className="mt-5" message={NO_EPISODES} />
      ) : (
        <div className="mt-5 flex flex-col gap-0.5">
          {anime.episodes.map((episode) => (
            <EpisodeRow
              key={episode.id}
              anime={anime}
              episode={episode}
              isAdmin={isAdmin}
              timezone={timezone}
            />
          ))}
        </div>
      )}
    </section>
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

/** Drops the parts the catalogue has no answer for, rather than showing gaps. */
function joinParts(parts: (string | null | undefined)[]): string {
  return parts
    .filter((part): part is string => part !== null && part !== undefined && part !== '')
    .join(' · ')
}

/**
 * "TV · 23 episodes · 2021" for a related show Arc has cached, and
 * "TV · Sequel" for one it does not — where there are no counts to give, the
 * relation itself is the most informative thing left to say.
 */
function relationKind(relation: AnimeRelation): string {
  const counts = joinParts([
    relation.episodes === null || relation.episodes === undefined
      ? null
      : `${String(relation.episodes)} episodes`,
    relation.season_year === null || relation.season_year === undefined
      ? null
      : String(relation.season_year),
  ])
  return counts === ''
    ? joinParts([relation.format, humanize(relation.relation_type)])
    : joinParts([relation.format, counts])
}

interface FranchiseCard {
  key: string
  /** Arc's page for it, or null when nothing has pulled the title in yet. */
  to: string | null
  title: string
  /** "TV · Sequel", or the counts for the show being viewed. */
  kind: string
  art: string | null
  /** Watch order, or "—" for a title that sits outside the line. */
  order: string
  current: boolean
}

/** Relations that come before this show, and the ones that come after. */
const BEFORE = new Set(['PREQUEL', 'PARENT'])
const AFTER = new Set(['SEQUEL'])

/**
 * "The franchise, in order" (the design's anime-specific rail).
 *
 * Arc holds a flat list of direct relations, not a chain, so the order it can
 * honestly state is: what precedes this show, this show, what follows it.
 * Everything else — side stories, spin-offs, recaps, alternative versions —
 * carries "—" rather than a number it has not earned, and keeps its relation
 * type in the kind line, which is the only thing the catalogue knows about it
 * beyond the format.
 */
function franchiseCards(anime: AnimeDetail): FranchiseCard[] {
  const before = anime.relations.filter((relation) => BEFORE.has(relation.relation_type))
  const after = anime.relations.filter((relation) => AFTER.has(relation.relation_type))
  const aside = anime.relations.filter(
    (relation) => !BEFORE.has(relation.relation_type) && !AFTER.has(relation.relation_type),
  )

  function card(relation: AnimeRelation, order: string): FranchiseCard {
    const id = relation.anime_id ?? relation.id
    return {
      key: relationKey(relation),
      to: id === null || id === undefined ? null : `/anime/${String(id)}`,
      title: relation.title.preferred,
      kind: relationKind(relation),
      art: relation.cover_large_url ?? relation.cover_url ?? null,
      order,
      current: false,
    }
  }

  const ownKind = joinParts([
    anime.format,
    anime.episode_count === null ? null : `${String(anime.episode_count)} episodes`,
    anime.season_year === null ? null : String(anime.season_year),
  ])

  return [
    ...before.map((relation, index) => card(relation, String(index + 1))),
    {
      key: 'current',
      to: null,
      title: anime.title.preferred,
      kind: ownKind,
      art: keyVisual(anime),
      order: String(before.length + 1),
      current: true,
    },
    ...after.map((relation, index) => card(relation, String(before.length + 2 + index))),
    ...aside.map((relation) => card(relation, '—')),
  ]
}

function FranchiseTile({ card }: { card: FranchiseCard }) {
  return (
    <article className="relative w-[160px]">
      <Artwork url={card.art} shape="key" className="w-full">
        <span className="absolute top-2.5 left-2.5 inline-flex h-[22px] items-center rounded-full border-[0.5px] border-[var(--arc-border-strong)] bg-[rgba(6,9,14,0.66)] px-[9px] text-[11px] tabular-nums text-white backdrop-blur-chip">
          {card.order}
        </span>
      </Artwork>
      <p className="mt-3 text-[15px] leading-tight font-medium text-pretty text-[var(--arc-text)]">
        {card.to === null ? (
          <span title={card.current ? undefined : UNLINKED_RELATION_HINT}>{card.title}</span>
        ) : (
          <Link
            to={card.to}
            className={cx('after:absolute after:inset-0 after:rounded-art', FOCUS_RING)}
          >
            {card.title}
          </Link>
        )}
      </p>
      <p className="mt-1 text-[13px] text-[var(--arc-text-muted)]">
        {card.current ? `${card.kind === '' ? '' : `${card.kind} · `}This show` : card.kind}
      </p>
    </article>
  )
}

function Franchise({ anime }: { anime: AnimeDetail }) {
  if (anime.relations.length === 0) return null

  return (
    <Shelf
      title="The franchise, in order"
      lede={FRANCHISE_LEDE}
      className="mt-16"
      scrollerClassName="gap-5"
    >
      {franchiseCards(anime).map((card) => (
        <FranchiseTile key={card.key} card={card} />
      ))}
    </Shelf>
  )
}

/**
 * "Made by" — the studio as an auteur credit, and whatever staff the
 * catalogue published. MAL publishes none, so the section is often just the
 * studio, and nothing at all when even that is unknown.
 */
function credits(anime: AnimeDetail): AnimeCredit[] {
  const sent = anime.credits ?? []
  const hasStudio = sent.some((credit) => credit.role.toLowerCase() === 'studio')
  if (hasStudio || anime.studio === null) return sent
  return [{ role: 'Studio', name: anime.studio }, ...sent]
}

function MadeBy({ anime }: { anime: AnimeDetail }) {
  const rows = credits(anime)
  if (rows.length === 0) return null

  return (
    <section className="mt-16 max-w-[720px]">
      <h2 className="text-[24px] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
        Made by
      </h2>
      <div className="mt-5 flex flex-col">
        {rows.map((credit) => (
          <div
            key={`${credit.role}:${credit.name}`}
            className="flex items-baseline gap-5 border-t-[0.5px] border-[var(--arc-border)] py-3.5"
          >
            <span className="w-[190px] shrink-0 text-[15px] text-[var(--arc-text-muted)]">
              {credit.role}
            </span>
            <span className="min-w-0 flex-1 text-[16px] text-[var(--arc-text)]">{credit.name}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

/** The hero, its title block, and the row of actions under it. */
function Hero({ anime, timezone }: { anime: AnimeDetail; timezone?: string }) {
  const alternatives = altTitles(anime)
  const entry = anime.list_entry
  const playable = playableEpisode(anime.episodes)

  return (
    <section>
      <HeroFrame banner={bannerArt(anime)} poster={keyVisual(anime)}>
        <h1 className="text-[clamp(30px,6vw,44px)] leading-[1.06] font-semibold tracking-[-0.03em] text-pretty text-white">
          {anime.title.preferred}
        </h1>
        {alternatives === '' ? null : (
          <p className="mt-2 font-jp text-[17px] tracking-[0.02em] text-[rgba(235,239,245,0.78)]">
            {alternatives}
          </p>
        )}
      </HeroFrame>

      <div className="mt-[22px] flex flex-wrap items-start gap-3">
        {playable === null ? (
          <Button disabled>{NOTHING_READY}</Button>
        ) : (
          <Link to={`/watch/${String(playable.id)}`} className={buttonClass('primary')}>
            <PlayGlyph />
            {`Play episode ${String(playable.number)}`}
          </Link>
        )}
        <ListStatusControl
          animeId={anime.id}
          status={anime.list_status}
          className={PILL_WRAP_FORCED}
        />
        {entry === null ? null : <ScoreControl animeId={anime.id} score={entry.score} />}
      </div>

      {anime.synopsis === null || anime.synopsis === '' ? null : (
        <p className="mt-[22px] max-w-[74ch] text-[16px] leading-[1.6] whitespace-pre-line text-[rgba(235,239,245,0.85)]">
          {anime.synopsis}
        </p>
      )}

      <p className="mt-3 text-[14px] text-[var(--arc-text-muted)]">{metaLine(anime)}</p>

      {anime.source === 'mal' ? (
        <p className="mt-1.5 text-[14px] text-[var(--arc-text-muted)]">{MAL_FALLBACK_NOTICE}</p>
      ) : null}

      {anime.next_airing === null ? null : (
        <p className="mt-1.5 text-[14px] text-[var(--arc-text-muted)]">
          {`Next episode: ${String(anime.next_airing.episode)} airs ${formatAirDate(anime.next_airing.at, timezone)}`}
        </p>
      )}

      {entry === null ? null : (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1">
          <p className="text-[14px] text-[var(--arc-text-muted)]">
            {`Watched ${String(entry.progress)} / ${anime.episode_count === null ? '?' : String(anime.episode_count)}`}
          </p>
          <MalSyncIndicator sync={entry.mal_sync} />
        </div>
      )}

      {anime.genres.length === 0 ? null : (
        <ul className="mt-3.5 flex flex-wrap gap-2">
          {anime.genres.map((genre) => (
            <li
              key={genre}
              className="rounded-full border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-1 text-[13px] text-[var(--arc-text-muted)]"
            >
              {genre}
            </li>
          ))}
        </ul>
      )}

      <ExternalLinks anime={anime} />
    </section>
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
      <>
        <Skeleton shape="hero" />
        <Skeleton shape="row" count={5} label={null} className="mt-14" />
      </>
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

  return (
    <div>
      <Hero anime={anime} timezone={timezone} />
      <Episodes anime={anime} isAdmin={me?.role === 'admin'} timezone={timezone} />
      <Franchise anime={anime} />
      <MadeBy anime={anime} />
    </div>
  )
}

function NotFoundState() {
  return (
    <section className="max-w-[74ch]">
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        Show not found
      </h1>
      <p className="mt-4 text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
        Arc has no anime with that id.
      </p>
      <Link className={buttonClass('secondary', 'mt-7')} to="/search">
        Back to search
      </Link>
    </section>
  )
}
