import { useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ErrorState } from '@/components/ErrorState'
import { isSearchable, summaryLine, type AnimeSummary } from '@/lib/anime'
import { formatRelativeTime } from '@/lib/recs'
import {
  directoryLabel,
  formatConfidence,
  formatScore,
  formatSize,
  parseEpisodeInput,
  parsedChips,
  REVIEW_TAB_LABELS,
  REVIEW_TABS,
  reviewErrorMessage,
  startingEpisode,
  SUGGESTION_WAIT_MS,
  suggestionOf,
  suggestionsEnabled,
  useConfirmReview,
  useIgnoreReview,
  useReopenReview,
  useRequestSuggestion,
  useReviewQueue,
  useReviewSearch,
  type ReviewCandidate,
  type ReviewItem,
  type ReviewSuggestion,
  type ReviewTab,
} from '@/lib/review'

/** What this queue is for, said once, above it. */
const EXPLANATION =
  'Files Arc could not match confidently wait here instead of being linked to a guess. Nothing ' +
  'from them is prepared or played until a person says which show and which episode they are. ' +
  'Confirm one of the candidates, search for a title the matcher never proposed, set the episode ' +
  'number by hand, or mark the file as not anime.'

const EMPTY_PENDING = 'Nothing waiting. Every file Arc has seen was matched with enough confidence.'

const EMPTY_IGNORED =
  'Nothing ignored. Files marked “not anime” collect here in case you change your mind.'

const EMPTY_AUTO =
  'Nothing auto-linked yet. Files the matcher was sure about are listed here, so you can check ' +
  'what it did on its own.'

const NOTHING_CHOSEN = 'Nothing chosen yet — pick a candidate, a suggestion, or a search result.'

const SUGGESTION_NOTE_SUFFIX = ', never applied automatically'

const SUGGESTION_REQUESTED = 'Requested — refresh in a moment.'

/** What the matcher's parting sentence is answering, said before it. */
const WHY_LABEL = 'Why it is here:'

const CONFIDENCE_LABELS: Record<string, string> = {
  high: 'high confidence',
  medium: 'medium confidence',
  low: 'low confidence',
}

const primaryButtonClass =
  'rounded-md bg-[var(--arc-accent)] px-3 py-1.5 text-sm font-medium text-[var(--arc-accent-contrast)] transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

const buttonClass =
  'rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface-raised)] px-3 py-1.5 text-sm text-[var(--arc-text)] transition-colors hover:border-[var(--arc-accent)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

const smallButtonClass =
  'shrink-0 rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface-raised)] px-2 py-1 text-xs text-[var(--arc-text)] transition-colors hover:border-[var(--arc-accent)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

const inputClass =
  'rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-3 py-2 text-sm text-[var(--arc-text)] placeholder:text-[var(--arc-text-muted)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)]'

const cardClass =
  'rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-4 text-[var(--arc-text)]'

function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded border border-[var(--arc-border)] bg-[var(--arc-surface-raised)] px-1.5 py-0.5 text-xs text-[var(--arc-text-muted)]">
      {children}
    </span>
  )
}

/**
 * The filename, where it sits, how big it is and when Arc first saw it.
 *
 * `min-w-0 flex-1` because this is the growing column wherever it sits beside
 * a button, and `break-all` is confined to the two lines that hold a path: a
 * release name has no spaces to break at, and everything *else* on this page
 * is prose or a title, which must never be broken mid-word.
 */
function FileHeading({ item }: { item: ReviewItem }) {
  return (
    <div className="min-w-0 flex-1">
      <p className="font-mono text-sm break-all text-[var(--arc-text)]">{item.name}</p>
      <p className="mt-1 text-xs break-all text-[var(--arc-text-muted)]">
        {directoryLabel(item.directory)} · {formatSize(item.size)} ·{' '}
        {formatRelativeTime(item.created_at)}
      </p>
    </div>
  )
}

/**
 * A show as every list on this page shows it: a fixed-width cover, then one
 * text column holding everything else.
 *
 * The column is the important half. It is `min-w-0 flex-1`, and anything a
 * row wants to say about the show — a score, the matcher's reasons — goes
 * *inside* it as `children` rather than beside it as another flex item. A
 * sibling with a width of its own (a `w-full` line of reasons, say) competes
 * with this column for the row and squeezes it to nothing, which renders a
 * title one character per line.
 */
function ShowLine({
  anime,
  width = 'w-10',
  children,
}: {
  anime: AnimeSummary
  width?: string
  children?: ReactNode
}) {
  const secondary = summaryLine(anime)

  return (
    <>
      <Link to={`/anime/${String(anime.id)}`} className={`block shrink-0 ${width}`}>
        <CoverThumb url={anime.cover_url} className="aspect-[2/3] w-full rounded" />
      </Link>
      <div className="min-w-0 flex-1">
        <Link
          to={`/anime/${String(anime.id)}`}
          className="text-sm leading-snug font-medium break-words text-[var(--arc-text)] hover:text-[var(--arc-accent)]"
        >
          {anime.title.preferred}
        </Link>
        {secondary === '' ? null : (
          <p className="mt-0.5 text-xs text-[var(--arc-text-muted)]">{secondary}</p>
        )}
        {children}
      </div>
    </>
  )
}

/**
 * What a model proposed for this file (FR-L5).
 *
 * Visually apart from the candidates and labelled as a suggestion, because it
 * is the one thing on the card that no rule produced. "Use this" fills the
 * form below and stops there: applying it is a person pressing Confirm, and
 * that separation is the requirement, not a nicety.
 */
function SuggestionBox({
  suggestion,
  onUse,
}: {
  suggestion: ReviewSuggestion
  onUse: (anime: AnimeSummary, episode: number | null) => void
}) {
  const model = suggestion.model === null || suggestion.model === '' ? null : suggestion.model
  const note = `from ${model ?? 'a language model'}${SUGGESTION_NOTE_SUFFIX}`

  return (
    <section className="mt-4 rounded-lg border border-dashed border-[var(--arc-accent)] bg-[var(--arc-surface-raised)] p-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h3 className="text-sm font-semibold text-[var(--arc-text)]">Suggestion</h3>
        <span className="text-xs text-[var(--arc-text-muted)]">{note}</span>
      </div>

      {suggestion.error !== null && suggestion.error !== '' ? (
        <p className="mt-2 text-sm text-[var(--arc-text-muted)]">
          No suggestion: {suggestion.error}
        </p>
      ) : suggestion.anime === null ? (
        <p className="mt-2 text-sm text-[var(--arc-text-muted)]">
          No suggestion: the model named no show.
        </p>
      ) : (
        <>
          <div className="mt-2 flex items-start gap-3">
            <ShowLine anime={suggestion.anime} width="w-12" />
            <button
              type="button"
              className={smallButtonClass}
              onClick={() => {
                if (suggestion.anime !== null) onUse(suggestion.anime, suggestion.episode_number)
              }}
            >
              Use this
            </button>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {suggestion.episode_number === null ? null : (
              <Chip>Episode {String(suggestion.episode_number)}</Chip>
            )}
            {/* A model that named no confidence gets no chip invented for it. */}
            {suggestion.confidence === null ? null : (
              <Chip>{CONFIDENCE_LABELS[suggestion.confidence] ?? suggestion.confidence}</Chip>
            )}
          </div>
          {suggestion.reason === null || suggestion.reason === '' ? null : (
            <p className="mt-2 text-sm leading-relaxed text-[var(--arc-text)]">
              {suggestion.reason}
            </p>
          )}
        </>
      )}
    </section>
  )
}

/** One row of the matcher's own shortlist, with the reasons it scored it. */
function CandidateRow({
  candidate,
  onChoose,
}: {
  candidate: ReviewCandidate
  onChoose: (anime: AnimeSummary, episode: number | null) => void
}) {
  const { anime } = candidate

  // Not a candidate at all: the matcher's own sentence about why this file is
  // in the queue. Labelled, because "below the auto-link threshold" on its own
  // under a list of shows reads as a comment on the last one.
  if (anime === null) {
    const reason = (candidate.reason ?? '').trim()
    if (reason === '') return null
    return (
      <li className="text-sm text-[var(--arc-text-muted)]">
        {WHY_LABEL} {reason}
      </li>
    )
  }

  const score = formatScore(candidate.score)
  const reasons = [
    ...candidate.reasons,
    ...(candidate.absolute ? ['absolute numbering'] : []),
  ].join(' · ')

  // Three flex items and no more: a fixed-width cover, the text column that
  // takes whatever is left, and the button. The score and the reasons go
  // *inside* the column — as siblings of it they each claim width of their
  // own, and the title is left a few pixels to wrap in.
  return (
    <li className="flex items-start gap-3 rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface-raised)] p-2">
      <ShowLine anime={anime}>
        {score === '' && reasons === '' ? null : (
          <p className="mt-0.5 text-xs text-[var(--arc-text-muted)]">
            {score === '' ? null : <span className="text-[var(--arc-text)]">{score}</span>}
            {score !== '' && reasons !== '' ? ' · ' : null}
            {reasons}
          </p>
        )}
      </ShowLine>
      <button
        type="button"
        aria-label={`Choose ${anime.title.preferred}`}
        className={`self-start ${smallButtonClass}`}
        onClick={() => {
          onChoose(anime, candidate.episode_number)
        }}
      >
        Choose
      </button>
    </li>
  )
}

interface PendingCardProps {
  item: ReviewItem
  /** False when the server will not ask a model at all, so nothing offers it. */
  canSuggest: boolean
  /** True between asking for a suggestion and its arrival. */
  waiting: boolean
  onRequested: (id: number) => void
  onDone: (message: string) => void
}

/**
 * One unsure file, and every way out of it (FR-L6).
 *
 * The card is ordered the way the decision is made: what the file says about
 * itself, what a model thinks, what the matcher scored, a search for anything
 * neither found, and last the form that commits — with the episode number
 * always editable, because "set the episode number manually" is half of what
 * this page is for and a candidate's guess at it is still a guess.
 */
function PendingCard({ item, canSuggest, waiting, onRequested, onDone }: PendingCardProps) {
  const [chosen, setChosen] = useState<AnimeSummary | null>(null)
  const [episode, setEpisode] = useState(() =>
    item.parsed.episode === null ? '' : String(item.parsed.episode),
  )
  const [search, setSearch] = useState('')

  const confirm = useConfirmReview()
  const ignore = useIgnoreReview()
  const suggest = useRequestSuggestion()
  const results = useReviewSearch(item.id, search)

  const suggestion = suggestionOf(item)
  const chips = parsedChips(item.parsed)
  const episodeNumber = parseEpisodeInput(episode)
  const canConfirm = chosen !== null && episodeNumber !== null

  function choose(anime: AnimeSummary, candidateEpisode: number | null) {
    setChosen(anime)
    const start = startingEpisode(candidateEpisode, item.parsed)
    setEpisode(start === null ? '' : String(start))
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (chosen === null || episodeNumber === null) return
    const title = chosen.title.preferred
    confirm.mutate(
      { id: item.id, anime_id: chosen.id, episode_number: episodeNumber },
      {
        onSuccess: () => {
          onDone(`${item.name} → ${title}, episode ${String(episodeNumber)}.`)
        },
      },
    )
  }

  // Gated on the box rather than on the query: `keepPreviousData` is what
  // stops the list flickering between two typed words, and without this it
  // would also leave the last results sitting under an emptied box.
  const searchResults = isSearchable(search) ? (results.data?.results ?? []) : []

  return (
    <article aria-label={item.name} className={cardClass}>
      <FileHeading item={item} />

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {chips.map((chip, index) => (
          <Chip key={`${chip}-${String(index)}`}>{chip}</Chip>
        ))}
        <span className="text-xs text-[var(--arc-text-muted)]">
          {formatConfidence(item.confidence)}
        </span>
      </div>

      {suggestion === null ? (
        canSuggest ? (
          <div className="mt-3">
            {waiting || suggest.isSuccess ? (
              <p role="status" className="text-xs text-[var(--arc-text-muted)]">
                {SUGGESTION_REQUESTED}
              </p>
            ) : (
              <button
                type="button"
                className={smallButtonClass}
                disabled={suggest.isPending}
                onClick={() => {
                  suggest.mutate(item.id, {
                    onSuccess: () => {
                      onRequested(item.id)
                    },
                  })
                }}
              >
                Ask for a suggestion
              </button>
            )}
            {suggest.isError ? (
              <p role="alert" className="mt-1 text-xs text-[var(--arc-error)]">
                {reviewErrorMessage(suggest.error)}
              </p>
            ) : null}
          </div>
        ) : null
      ) : (
        <SuggestionBox suggestion={suggestion} onUse={choose} />
      )}

      <section className="mt-4">
        <h3 className="text-sm font-semibold text-[var(--arc-text)]">Candidates</h3>
        {item.candidates.length === 0 ? (
          <p className="mt-1 text-sm text-[var(--arc-text-muted)]">
            The matcher proposed nothing. Search for the title below.
          </p>
        ) : (
          <ul className="mt-2 flex flex-col gap-2">
            {item.candidates.map((candidate, index) => (
              <CandidateRow
                key={candidate.anime === null ? `reason-${String(index)}` : candidate.anime.id}
                candidate={candidate}
                onChoose={choose}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="mt-4">
        <label
          htmlFor={`review-search-${String(item.id)}`}
          className="block text-sm font-semibold text-[var(--arc-text)]"
        >
          Search another title
        </label>
        <input
          id={`review-search-${String(item.id)}`}
          type="search"
          autoComplete="off"
          maxLength={100}
          placeholder="Title…"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
          }}
          className={`mt-1 w-full max-w-md ${inputClass}`}
        />
        {results.isError ? (
          <p role="alert" className="mt-1 text-xs text-[var(--arc-error)]">
            {reviewErrorMessage(results.error)}
          </p>
        ) : null}
        {searchResults.length === 0 ? null : (
          <ul className="mt-2 flex flex-col gap-1">
            {searchResults.map((anime) => (
              <li
                key={anime.id}
                className="flex items-center gap-2 rounded-md border border-[var(--arc-border)] px-2 py-1"
              >
                <span className="min-w-0 flex-1 truncate text-sm text-[var(--arc-text)]">
                  {anime.title.preferred}
                </span>
                <span className="shrink-0 text-xs text-[var(--arc-text-muted)]">
                  {summaryLine(anime)}
                </span>
                <button
                  type="button"
                  aria-label={`Choose ${anime.title.preferred} from search results`}
                  className={smallButtonClass}
                  onClick={() => {
                    choose(anime, null)
                  }}
                >
                  Choose
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <form className="mt-4 border-t border-[var(--arc-border)] pt-3" onSubmit={submit}>
        <p className="text-sm text-[var(--arc-text-muted)]">
          {chosen === null ? (
            NOTHING_CHOSEN
          ) : (
            <>
              Confirming as{' '}
              <span className="font-medium text-[var(--arc-text)]">{chosen.title.preferred}</span>
            </>
          )}
        </p>

        <div className="mt-2 flex flex-wrap items-end gap-3">
          <div>
            <label
              htmlFor={`review-episode-${String(item.id)}`}
              className="block text-xs text-[var(--arc-text-muted)]"
            >
              Episode number
            </label>
            <input
              id={`review-episode-${String(item.id)}`}
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={episode}
              onChange={(event) => {
                setEpisode(event.target.value)
              }}
              className={`mt-1 w-24 ${inputClass}`}
            />
          </div>

          <button
            type="submit"
            className={primaryButtonClass}
            disabled={!canConfirm || confirm.isPending}
          >
            Confirm
          </button>

          <button
            type="button"
            className={buttonClass}
            disabled={ignore.isPending}
            onClick={() => {
              ignore.mutate(item.id, {
                onSuccess: () => {
                  onDone(`${item.name} marked as not anime.`)
                },
              })
            }}
          >
            Ignore (not anime)
          </button>
        </div>

        {confirm.isError ? (
          <p role="alert" className="mt-2 text-sm text-[var(--arc-error)]">
            {reviewErrorMessage(confirm.error)}
          </p>
        ) : null}
        {ignore.isError ? (
          <p role="alert" className="mt-2 text-sm text-[var(--arc-error)]">
            {reviewErrorMessage(ignore.error)}
          </p>
        ) : null}
      </form>
    </article>
  )
}

/** An ignored file: out of the way, with the one way back. */
function IgnoredCard({ item, onDone }: { item: ReviewItem; onDone: (message: string) => void }) {
  const reopen = useReopenReview()
  const chips = parsedChips(item.parsed)

  return (
    <article aria-label={item.name} className={cardClass}>
      {/* Same shape as a candidate row: one growing text column, one button
          that never gives up its own width to it. */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <FileHeading item={item} />
        <button
          type="button"
          className={`shrink-0 ${buttonClass}`}
          disabled={reopen.isPending}
          onClick={() => {
            reopen.mutate(item.id, {
              onSuccess: () => {
                onDone(`${item.name} is back in the queue.`)
              },
            })
          }}
        >
          Reopen
        </button>
      </div>
      {chips.length === 0 ? null : (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {chips.map((chip, index) => (
            <Chip key={`${chip}-${String(index)}`}>{chip}</Chip>
          ))}
        </div>
      )}
      {reopen.isError ? (
        <p role="alert" className="mt-2 text-sm text-[var(--arc-error)]">
          {reviewErrorMessage(reopen.error)}
        </p>
      ) : null}
    </article>
  )
}

/**
 * What the matcher linked without asking, read-only (FR-L4).
 *
 * Here for trust rather than for action: an auto-link is not something to
 * undo from this page — the file is linked, possibly transcoded and possibly
 * half-watched, and unlinking it belongs with the admin tools — but a person
 * ought to be able to see what the threshold let through.
 */
function AutoRow({ item }: { item: ReviewItem }) {
  const linked = item.candidates.find((candidate) => candidate.anime !== null)
  const anime = linked?.anime ?? null
  const number = linked?.episode_number ?? item.parsed.episode

  return (
    <li aria-label={item.name} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-1 py-2">
      {/* `break-all` belongs to the filename and stops there. The show beside
          it keeps its own width — the row wraps rather than squeezing it. */}
      <span className="min-w-0 flex-1 basis-64 font-mono text-xs break-all text-[var(--arc-text-muted)]">
        {item.name}
      </span>
      <span className="shrink-0 text-sm break-words text-[var(--arc-text)]">
        {anime === null ? (
          // Nothing in the row names the show: the candidate list was cleared,
          // or the file predates it. Saying which episode it went to is still
          // more than saying nothing.
          item.episode_id === null ? (
            'Linked'
          ) : (
            `Linked to episode #${String(item.episode_id)}`
          )
        ) : (
          <Link to={`/anime/${String(anime.id)}`} className="hover:text-[var(--arc-accent)]">
            {anime.title.preferred}
          </Link>
        )}
        {number === null ? null : (
          <span className="text-[var(--arc-text-muted)]"> · episode {String(number)}</span>
        )}
      </span>
    </li>
  )
}

/**
 * Match review (spec §4.3 FR-L4/FR-L5/FR-L6, roadmap M13).
 *
 * The whole page is one queue and the decisions taken out of it. It holds
 * three things itself: which state is being listed, the one-line confirmation
 * left behind when a card resolves and vanishes, and the deadline that keeps
 * the queue polling while a requested suggestion is still being generated —
 * the answer arrives in a job, so re-asking is the only way to see it, and the
 * deadline is what stops that being forever.
 */
export function Review() {
  const [tab, setTab] = useState<ReviewTab>('pending')
  const [notice, setNotice] = useState<string | null>(null)
  const [wait, setWait] = useState<{ ids: number[]; until: number } | null>(null)

  const { data, error, isPending, isError, isFetching, refetch } = useReviewQueue(
    tab,
    wait?.until ?? null,
  )

  const items = data?.items ?? []
  // Stop polling the moment every asked-for suggestion has landed (or its
  // card has gone), rather than waiting out the deadline. Adjusting state
  // during render is React's own answer to "derive state from props"; it
  // re-renders before committing, so nothing flashes.
  if (
    wait !== null &&
    !wait.ids.some((id) => {
      const item = items.find((candidate) => candidate.id === id)
      return item !== undefined && suggestionOf(item) === null
    })
  ) {
    setWait(null)
  }

  function requested(id: number) {
    setWait((current) => ({
      ids: current === null ? [id] : [...current.ids, id],
      until: Date.now() + SUGGESTION_WAIT_MS,
    }))
  }

  function tabLabel(value: ReviewTab): string {
    const label = REVIEW_TAB_LABELS[value]
    return value === 'pending' && data !== undefined ? `${label} (${String(data.pending)})` : label
  }

  return (
    <section className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">Match review</h1>
      <p className="mt-2 max-w-3xl text-sm leading-relaxed text-[var(--arc-text-muted)]">
        {EXPLANATION}
      </p>

      <div className="mt-4 flex flex-wrap gap-2">
        {REVIEW_TABS.map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={value === tab}
            className={`rounded-md border px-3 py-1.5 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] ${
              value === tab
                ? 'border-[var(--arc-accent)] bg-[var(--arc-surface-raised)] text-[var(--arc-text)]'
                : 'border-[var(--arc-border)] bg-[var(--arc-surface)] text-[var(--arc-text-muted)] hover:border-[var(--arc-accent)]'
            }`}
            onClick={() => {
              setNotice(null)
              setTab(value)
            }}
          >
            {tabLabel(value)}
          </button>
        ))}
      </div>

      {notice === null ? null : (
        <p role="status" className="mt-4 text-sm text-[var(--arc-ok)]">
          {notice}
        </p>
      )}

      {isPending ? (
        <p role="status" className="mt-6 text-sm text-[var(--arc-text-muted)]">
          Loading…
        </p>
      ) : isError ? (
        <ErrorState
          className="mt-6"
          message={reviewErrorMessage(error)}
          pending={isFetching}
          onRetry={() => {
            void refetch()
          }}
        />
      ) : items.length === 0 ? (
        <p className="mt-6 max-w-3xl text-sm text-[var(--arc-text-muted)]">
          {tab === 'pending' ? EMPTY_PENDING : tab === 'ignored' ? EMPTY_IGNORED : EMPTY_AUTO}
        </p>
      ) : tab === 'auto' ? (
        <ul className="mt-6 divide-y divide-[var(--arc-border)] rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-1">
          {items.map((item) => (
            <AutoRow key={item.id} item={item} />
          ))}
        </ul>
      ) : (
        <div className="mt-6 flex flex-col gap-4">
          {items.map((item) =>
            tab === 'ignored' ? (
              <IgnoredCard key={item.id} item={item} onDone={setNotice} />
            ) : (
              <PendingCard
                key={item.id}
                item={item}
                canSuggest={suggestionsEnabled(data)}
                waiting={wait?.ids.includes(item.id) ?? false}
                onRequested={requested}
                onDone={setNotice}
              />
            ),
          )}
        </div>
      )}
    </section>
  )
}
