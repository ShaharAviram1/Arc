import { useEffect, useId, useRef, useState } from 'react'
import {
  buttonClass,
  cx,
  FIELD_ERROR_CLASS,
  FOCUS_RING,
  inputClass,
  LABEL_CLASS,
} from '@/components/ui'
import {
  candidateFacts,
  releaseErrorMessage,
  useChooseRelease,
  useReleases,
  type CurrentRelease,
  type ReleaseCandidate,
  type ReleaseScope,
} from '@/lib/releases'

/**
 * "Change release…" for an episode, "Choose pack…" for a trip (spec FR-A13).
 *
 * An in-page sheet: what is downloading now at the top, then the releases Arc
 * found on Nyaa for this — the ones Arc would take first, in its own order,
 * then the rest with the sentence it would not take them for — then a box for
 * a magnet or a Nyaa link, and one button. Whatever is chosen replaces what
 * the episode (or the trip's episodes) were downloading from; the server
 * refuses what it must, in sentences, and they are shown as they come.
 *
 * Not a "download anything" door (spec §8): it only ever opens for an episode
 * the viewer already wants, or for their own trip.
 */

const SEARCHING = 'Looking on Nyaa… this can take up to a minute.'
const NOTHING_FOUND = 'Nyaa has nothing for this right now. You can still paste a link.'
const LIST_FAILED = 'Could not load the releases.'
const CHOOSE_FAILED = 'Could not use that release.'
const PASTE_LABEL = 'Or paste a magnet link or a Nyaa link'
const PASTE_HINT = 'A nyaa.si page or .torrent link, or a magnet link Nyaa lists for this.'
const USE = 'Use this release'

function percent(value: number | null): string | null {
  if (value === null) return null
  return `${String(Math.round(value * 100))}%`
}

function CurrentLine({ current }: { current: CurrentRelease | null | undefined }) {
  if (current === undefined) return null
  if (current === null) {
    return <p className="text-[14px] text-[var(--arc-text-muted)]">Nothing is downloading yet.</p>
  }
  const facts = [
    current.kind === 'batch' ? 'Pack' : 'Single',
    percent(current.progress),
    current.manual ? 'chosen by hand' : null,
  ].filter((part): part is string => part !== null)
  return (
    <div>
      <p className={LABEL_CLASS}>Downloading now</p>
      <p className="mt-1 text-[14px] break-words text-[var(--arc-text)]">
        {current.title ?? 'Unnamed release'}
      </p>
      <p className="mt-0.5 text-[13px] text-[var(--arc-text-muted)]">{facts.join(' · ')}</p>
    </div>
  )
}

function CandidateRow({
  candidate,
  name,
  checked,
  onPick,
}: {
  candidate: ReleaseCandidate
  name: string
  checked: boolean
  onPick: () => void
}) {
  const id = useId()
  const current = candidate.current === true
  return (
    <li>
      <label
        htmlFor={id}
        className={cx(
          'flex cursor-pointer items-start gap-3 rounded-[12px] px-3 py-2.5 transition-colors',
          'hover:bg-[rgba(255,255,255,0.05)]',
          checked && 'bg-[rgba(255,255,255,0.07)]',
          current && 'cursor-default opacity-70 hover:bg-transparent',
        )}
      >
        <input
          id={id}
          type="radio"
          name={name}
          className="mt-1 shrink-0"
          checked={checked}
          disabled={current}
          onChange={onPick}
        />
        <span className="min-w-0 flex-1">
          <span
            className="block text-[14px] break-words text-[var(--arc-text)]"
            title={candidate.title}
          >
            {candidate.title}
          </span>
          <span className="mt-0.5 block text-[13px] text-[var(--arc-text-muted)] tabular-nums">
            {candidateFacts(candidate)}
          </span>
          {current ? (
            <span className="mt-0.5 block text-[13px] text-[var(--arc-text-muted)]">
              Downloading now
            </span>
          ) : candidate.reason !== null ? (
            <span className="mt-0.5 block text-[13px] text-[var(--arc-warn)]">
              {candidate.acceptable
                ? candidate.reason
                : `Arc would not take it: ${candidate.reason}`}
            </span>
          ) : null}
        </span>
      </label>
    </li>
  )
}

export function ReleaseSheet({
  scope,
  heading,
  onClose,
}: {
  scope: ReleaseScope
  heading: string
  onClose: () => void
}) {
  const headingId = useId()
  const pasteId = useId()
  const radioName = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const releases = useReleases(scope, true)
  const choose = useChooseRelease(scope)
  const [picked, setPicked] = useState<string | null>(null)
  const [link, setLink] = useState('')

  useEffect(() => {
    dialogRef.current?.focus()
  }, [])

  const trimmed = link.trim()
  const ready = (picked !== null || trimmed !== '') && !choose.isPending

  function submit() {
    if (!ready) return
    choose.mutate(trimmed !== '' ? { link: trimmed } : { candidate_id: picked ?? '' }, {
      onSuccess: () => {
        onClose()
      },
    })
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-[rgba(0,0,0,0.55)] sm:items-center"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.preventDefault()
            onClose()
          }
        }}
        className={cx(
          'rounded-card flex max-h-[90vh] w-full max-w-[640px] flex-col border-[0.5px] border-[var(--arc-border-strong)]',
          'bg-[var(--arc-bg)] shadow-bar outline-none',
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b-[0.5px] border-[var(--arc-border)] p-4 sm:p-5">
          <h2
            id={headingId}
            className="text-[18px] font-semibold tracking-[-0.01em] text-[var(--arc-text)]"
          >
            {heading}
          </h2>
          <button
            type="button"
            className={buttonClass('chip', 'h-9 px-4 text-[13px]')}
            onClick={onClose}
          >
            Close
          </button>
        </div>

        <form
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={(event) => {
            event.preventDefault()
            submit()
          }}
        >
          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
            <CurrentLine current={releases.data?.current} />

            <div className="mt-4">
              {releases.isPending ? (
                <p role="status" className="text-[14px] text-[var(--arc-text-muted)]">
                  {SEARCHING}
                </p>
              ) : releases.isError ? (
                <div className="flex flex-wrap items-center gap-2.5">
                  <p role="alert" className={FIELD_ERROR_CLASS}>
                    {releaseErrorMessage(releases.error, LIST_FAILED)}
                  </p>
                  <button
                    type="button"
                    className={buttonClass('chip', 'h-9 px-4 text-[13px]')}
                    onClick={() => {
                      void releases.refetch()
                    }}
                  >
                    Try again
                  </button>
                </div>
              ) : releases.data.candidates.length === 0 ? (
                <p className="text-[14px] text-[var(--arc-text-muted)]">{NOTHING_FOUND}</p>
              ) : (
                <fieldset>
                  <legend className={LABEL_CLASS}>Releases on Nyaa</legend>
                  <ul className="mt-1.5 flex flex-col gap-0.5" aria-label="Releases on Nyaa">
                    {releases.data.candidates.map((candidate) => (
                      <CandidateRow
                        key={candidate.id}
                        candidate={candidate}
                        name={radioName}
                        checked={picked === candidate.id && trimmed === ''}
                        onPick={() => {
                          setPicked(candidate.id)
                          setLink('')
                          choose.reset()
                        }}
                      />
                    ))}
                  </ul>
                </fieldset>
              )}
            </div>

            <div className="mt-5 flex flex-col gap-1">
              <label htmlFor={pasteId} className={LABEL_CLASS}>
                {PASTE_LABEL}
              </label>
              <input
                id={pasteId}
                type="text"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                value={link}
                placeholder="https://nyaa.si/view/…"
                className={inputClass('w-full')}
                onChange={(event) => {
                  setLink(event.target.value)
                  if (event.target.value.trim() !== '') setPicked(null)
                  choose.reset()
                }}
              />
              <p className="text-[13px] text-[var(--arc-text-muted)]">{PASTE_HINT}</p>
            </div>
          </div>

          <div className="flex flex-col gap-2 border-t-[0.5px] border-[var(--arc-border)] p-4 sm:p-5">
            {choose.isError ? (
              <p role="alert" className={FIELD_ERROR_CLASS}>
                {releaseErrorMessage(choose.error, CHOOSE_FAILED)}
              </p>
            ) : null}
            <div className="flex flex-wrap items-center gap-2.5">
              <button
                type="submit"
                disabled={!ready}
                className={buttonClass('primary', cx('px-5 text-[14px]', FOCUS_RING))}
              >
                {choose.isPending ? 'Starting…' : USE}
              </button>
              <p className="text-[13px] text-[var(--arc-text-muted)]">
                {scope.kind === 'trip'
                  ? 'It replaces the current downloads of these episodes.'
                  : 'It replaces what is downloading now.'}
              </p>
            </div>
          </div>
        </form>
      </div>
    </div>
  )
}

/** What the episode row's overflow menu offers (FR-A13). */
const CHANGE_RELEASE = 'Change release…'

/**
 * The episode row's overflow: one "⋯" button, one menu, one item for now.
 * Rendered by the row only for an episode the viewer wants and nothing has
 * landed for yet, and never for the demo account.
 */
export function EpisodeMoreMenu({
  animeId,
  episodeId,
  number,
}: {
  animeId: number
  episodeId: number
  number: number
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuId = useId()

  return (
    <span className="relative inline-flex">
      <button
        ref={buttonRef}
        type="button"
        aria-label={`More for episode ${String(number)}`}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-controls={menuOpen ? menuId : undefined}
        className={buttonClass('chip', 'w-11 px-0 text-[18px] leading-none')}
        onClick={() => {
          setMenuOpen(!menuOpen)
        }}
      >
        ⋯
      </button>
      {menuOpen ? (
        <div
          id={menuId}
          role="menu"
          data-offline-menu=""
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              setMenuOpen(false)
              buttonRef.current?.focus()
            }
          }}
          className={cx(
            'absolute top-full right-0 z-30 mt-2 w-[13rem] rounded-card border-[0.5px] p-1.5 text-left shadow-bar backdrop-blur-bar',
            'border-[var(--arc-border-strong)] bg-[rgba(18,23,34,0.98)]',
          )}
        >
          <button
            type="button"
            role="menuitem"
            className={cx(
              'flex min-h-11 w-full items-center rounded-[10px] px-3 text-[14px] text-[var(--arc-text)]',
              'hover:bg-[rgba(255,255,255,0.07)]',
              FOCUS_RING,
            )}
            onClick={() => {
              setMenuOpen(false)
              setSheetOpen(true)
            }}
          >
            {CHANGE_RELEASE}
          </button>
        </div>
      ) : null}
      {sheetOpen ? (
        <ReleaseSheet
          scope={{ kind: 'episode', id: episodeId, animeId, number }}
          heading={`Change release · Episode ${String(number)}`}
          onClose={() => {
            setSheetOpen(false)
            buttonRef.current?.focus()
          }}
        />
      ) : null}
    </span>
  )
}

/** The trip panel's "Choose pack…" (FR-A13): the same sheet, scoped to the trip. */
export function ChoosePackButton({ tripId, animeId }: { tripId: number; animeId: number }) {
  const [open, setOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={buttonClass('chip', 'h-9 px-4 text-[13px]')}
        onClick={() => {
          setOpen(true)
        }}
      >
        Choose pack…
      </button>
      {open ? (
        <ReleaseSheet
          scope={{ kind: 'trip', id: tripId, animeId }}
          heading="Choose a pack for this trip"
          onClose={() => {
            setOpen(false)
            buttonRef.current?.focus()
          }}
        />
      ) : null}
    </>
  )
}
