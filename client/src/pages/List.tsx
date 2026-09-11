import { useSearchParams } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import { Artwork, Chip, cx, EmptyState, Row, RowGroup, Skeleton } from '@/components/ui'
import {
  isListStatus,
  keyVisual,
  LIST_STATUS_LABELS,
  useMyList,
  type AnimeSummary,
  type ListStatus,
  type MyListItem,
} from '@/lib/anime'

/**
 * My List (spec §4.6 FR-W2, §5; design handoff "My List").
 *
 * The list Arc works from: anything set to Watching is what the acquisition
 * rules fetch episodes for, which is why that is the status the page opens on.
 * Rows rather than a grid, because the question this page answers is "how far
 * through am I", and a bar beside a title answers it in one pass where a wall
 * of covers does not.
 *
 * `?status=` is the source of truth for the filter, so a chip is shareable and
 * survives a refresh — the same rule Browse and MyAnimeList already follow.
 * The request is the existing `GET /api/list?status=…`; nothing here is new on
 * the wire.
 */

/** The design's order, which is the order a list is lived in. */
const STATUS_ORDER: readonly ListStatus[] = [
  'watching',
  'planned',
  'on_hold',
  'completed',
  'dropped',
]

const DEFAULT_STATUS: ListStatus = 'watching'

const LEDE =
  'Anything set to Watching is what Arc fetches episodes for. The list stays in step with ' +
  'MyAnimeList.'

const EMPTY_MESSAGES: Record<ListStatus, string> = {
  watching: 'Nothing on the go. Set a show to Watching and Arc starts fetching its episodes.',
  planned: 'Nothing planned. Shows you add from Browse or recommendations land here.',
  on_hold: 'Nothing on hold. This is where a show waits when you stop without dropping it.',
  completed: 'Nothing finished yet.',
  dropped: 'Nothing dropped.',
}

const LIST_ERROR = 'Could not load your list.'

/** AniList's airing states, in the words the design uses for the column. */
const AIRING_LABELS: Record<string, string> = {
  RELEASING: 'Airing',
  FINISHED: 'Finished',
  NOT_YET_RELEASED: 'Not yet aired',
  CANCELLED: 'Cancelled',
  HIATUS: 'On hiatus',
}

/**
 * The airing column. Only a show actually broadcasting is bright: it is the
 * one whose next episode is still coming, and the column exists to separate
 * "this one still moves" from "this one is a back catalogue".
 */
function airingState(anime: AnimeSummary): { label: string; live: boolean } | null {
  const { status } = anime
  if (status === null || status === '') return null
  return { label: AIRING_LABELS[status] ?? status, live: status === 'RELEASING' }
}

/**
 * "Episode 8 of 14 · Madhouse" — how far in, then who made it.
 *
 * The studio is the credit anime is actually shelved by, which is why the
 * design puts it here. A record that has none — anything filled from MAL —
 * falls back to what the card line has always said, format and year, rather
 * than leaving a separator with nothing after it.
 */
function rowMeta(item: MyListItem): string {
  const { anime, entry } = item
  const total = anime.episodes
  const parts = [
    total === null
      ? `Episode ${String(entry.progress)}`
      : `Episode ${String(entry.progress)} of ${String(total)}`,
  ]
  const studio = anime.studio ?? null
  if (studio !== null && studio !== '') {
    parts.push(studio)
  } else {
    if (anime.format !== null && anime.format !== '') parts.push(anime.format)
    if (anime.season_year !== null) parts.push(String(anime.season_year))
  }
  return parts.join(' · ')
}

/** How far through the season, 0–1, or null when the total is unknown. */
function seasonFraction(item: MyListItem): number | null {
  const total = item.anime.episodes
  if (total === null || total <= 0) return null
  return Math.min(1, Math.max(0, item.entry.progress / total))
}

function ListRow({ item }: { item: MyListItem }) {
  const fraction = seasonFraction(item)
  const percent = fraction === null ? null : Math.round(fraction * 100)
  const airing = airingState(item.anime)
  const meta = rowMeta(item)

  return (
    <Row to={`/anime/${String(item.anime.id)}`}>
      <Artwork url={keyVisual(item.anime)} shape="thumb" className="w-[46px] shrink-0" />

      <div className="min-w-0 flex-1">
        <p className="truncate text-[16px] font-medium text-[var(--arc-text)]">
          {item.anime.title.preferred}
        </p>
        <p className="truncate text-[13px] text-[var(--arc-text-muted)]">{meta}</p>
      </div>

      <div className="hidden w-[160px] shrink-0 items-center gap-3 sm:flex">
        <span
          role="progressbar"
          aria-label={`Season progress for ${item.anime.title.preferred}`}
          aria-valuenow={percent ?? undefined}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuetext={meta}
          className="block h-1 flex-1 overflow-hidden rounded-full bg-[rgba(255,255,255,0.16)]"
        >
          {/*
           * The one place in Arc the arc gradient appears: progress through a
           * season. Playback progress is plain white everywhere else.
           */}
          <span
            className="progress-arc block h-full"
            style={{ width: `${String((fraction ?? 0) * 100)}%` }}
          />
        </span>
        <span className="w-[42px] shrink-0 text-right text-[13px] tabular-nums text-[var(--arc-text-muted)]">
          {percent === null ? '—' : `${String(percent)}%`}
        </span>
      </div>

      {airing === null ? null : (
        <span
          className={cx(
            'hidden w-[124px] shrink-0 text-right text-[14px] md:block',
            airing.live ? 'text-[var(--arc-text)]' : 'text-[var(--arc-text-muted)]',
          )}
        >
          {airing.label}
        </span>
      )}
    </Row>
  )
}

export function List() {
  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get('status') ?? ''
  const status: ListStatus = isListStatus(raw) ? raw : DEFAULT_STATUS

  const { data, isError, isFetching, refetch } = useMyList(status)

  function select(next: ListStatus) {
    const params = new URLSearchParams(searchParams)
    // The default status is the bare URL: `/list` and `/list?status=watching`
    // are the same page, and only one of them should be in the history.
    if (next === DEFAULT_STATUS) params.delete('status')
    else params.set('status', next)
    setSearchParams(params, { replace: true })
  }

  return (
    <section>
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        My List
      </h1>
      <p className="mt-2.5 max-w-[66ch] text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
        {LEDE}
      </p>

      <div role="group" aria-label="List status" className="mt-6 flex flex-wrap gap-2.5">
        {STATUS_ORDER.map((value) => (
          <Chip
            key={value}
            active={value === status}
            onClick={() => {
              select(value)
            }}
          >
            {LIST_STATUS_LABELS[value]}
          </Chip>
        ))}
      </div>

      <div className="mt-7 max-w-[1080px]">
        {isError ? (
          <ErrorState
            message={LIST_ERROR}
            pending={isFetching}
            onRetry={() => {
              void refetch()
            }}
          />
        ) : data === undefined ? (
          <Skeleton shape="row" count={6} />
        ) : data.length === 0 ? (
          <EmptyState message={EMPTY_MESSAGES[status]} />
        ) : (
          <RowGroup>
            {data.map((item) => (
              <ListRow key={item.anime.id} item={item} />
            ))}
          </RowGroup>
        )}
      </div>
    </section>
  )
}
