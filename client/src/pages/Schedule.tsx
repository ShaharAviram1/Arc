import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import { ListStatusControl } from '@/components/ListStatusControl'
import {
  Artwork,
  buttonClass,
  cx,
  EmptyState,
  inputClass,
  Skeleton,
  FIELD_ERROR_CLASS,
  FOCUS_RING,
} from '@/components/ui'
import { catalogErrorMessage } from '@/lib/anime'
import { authErrorMessage, timezoneOptions, useUpdateTimezone } from '@/lib/auth'
import {
  parseSeason,
  parseYear,
  seasonLabel,
  useSchedule,
  weekdayInTimezone,
  WEEKDAY_LABELS,
  type ScheduleEntry,
  type SeasonRef,
} from '@/lib/schedule'

const EMPTY_SEASON = 'Nothing cached for this season yet — the catalogue sweep runs daily.'

const UNSCHEDULED_TITLE = 'Movies, OVAs and unscheduled'

/** Why a time carries "est." — same wording as the show page (FR-C6). */
const ESTIMATED_HINT = 'Estimated from the broadcast slot'

/** The caveat that travels with a MAL-sourced row (FR-C6). */
const VIA_MAL_HINT = 'AniList is unavailable; this row came from MyAnimeList'

/**
 * "20:00 est. · Ep 7", with the parts the server actually knows. The marker
 * sits against the time because that is what it qualifies: the episode number
 * is not a guess even when the moment it airs is.
 *
 * A `<span>` rather than a `<p>`: the whole row is one `<a>`, and a paragraph
 * inside a link is legal but reads oddly to anything walking the tree.
 */
function SlotLine({ entry }: { entry: ScheduleEntry }) {
  return (
    <span className="mt-0.5 block text-[12px] tabular-nums text-[var(--arc-text-muted)]">
      {entry.air_time_local ?? 'Time unknown'}
      {entry.next_at_estimated ? (
        <>
          {' '}
          <span title={ESTIMATED_HINT} className="italic">
            est.
          </span>
        </>
      ) : null}
      {entry.next_episode === null ? null : ` · Ep ${String(entry.next_episode)}`}
    </span>
  )
}

/**
 * One show in a day column: a 38px key visual, a two-line title and the slot.
 * Nothing else.
 *
 * A seven-column week is about 158px per column, and everything that used to
 * live in this row — a 112px status select, a bordered "via MAL" pill, a third
 * line of text — was competing for those 158px and losing: the select alone
 * took more width than the column had left, so titles broke one word per line
 * and the control sat on top of the artwork. The row is now the link, at the
 * two sizes that fit (13px title, 12px slot), and the caveat is the link's
 * `title` rather than a line of its own.
 *
 * The list-status control survives below `lg`, where the days stack and a row
 * is the full width of the page. On the seven-column grid it is one tap away
 * on the show page, which is the prototype's own answer ("the list-status
 * control moves into the row action sheet — reachable in one more tap").
 *
 * `aria-label` on the link so its accessible name stays the title alone; the
 * slot inside it is detail, not part of what the link is called.
 *
 * A followed show keeps the column's air-time order — the order is what a
 * person reads a schedule for (FR-C3) — and is marked by the grouped surface
 * arriving under it. The old accent edge went with the old palette: in this
 * design colour means state, and "on my list" is not a state wanting a colour.
 */
function EntryRow({
  entry,
  statusClassName = 'px-2 pb-2 lg:hidden',
}: {
  entry: ScheduleEntry
  /**
   * Where the list-status control is allowed to appear. The week grid hides it
   * from `lg` up (there is no room); the unscheduled list, which is never
   * seven columns wide, keeps it at every size.
   */
  statusClassName?: string
}) {
  const { anime } = entry
  const href = `/anime/${String(anime.id)}`

  return (
    <li
      data-following={entry.following ? 'true' : undefined}
      className={cx(
        'rounded-row transition-colors duration-200',
        entry.following
          ? 'bg-[var(--arc-surface)]'
          : 'hover:bg-[color-mix(in_srgb,var(--arc-surface)_60%,transparent)]',
      )}
    >
      <Link
        to={href}
        aria-label={anime.title.preferred}
        title={anime.source === 'mal' ? VIA_MAL_HINT : undefined}
        className={cx('flex items-center gap-2.5 rounded-row p-2', FOCUS_RING)}
      >
        <span className="block w-[38px] shrink-0">
          <Artwork url={anime.cover_url} shape="thumb" />
        </span>
        <span className="block min-w-0 flex-1">
          {/* No `block` here: `line-clamp-2` is `display:-webkit-box`, and a
              `display` utility beside it silently wins and un-clamps the
              title. `break-words` is for the single long word a Japanese
              title transliterates into, which would otherwise run out of the
              column rather than wrap inside it. */}
          <span className="line-clamp-2 text-[13px] leading-snug font-medium break-words text-[var(--arc-text)]">
            {anime.title.preferred}
          </span>
          <SlotLine entry={entry} />
        </span>
      </Link>

      <ListStatusControl
        animeId={anime.id}
        status={entry.list_status}
        label={`List status for ${anime.title.preferred}`}
        className={statusClassName}
      />
    </li>
  )
}

/**
 * One weekday. On a phone the days stack and today comes first, because the
 * question a schedule answers on a phone is "what is on tonight"; on a wide
 * screen the seven columns stay in weekday order, which is what makes the grid
 * readable as a week.
 */
function DayColumn({
  weekday,
  entries,
  isToday,
}: {
  weekday: number
  entries: ScheduleEntry[]
  isToday: boolean
}) {
  const label = WEEKDAY_LABELS[weekday] ?? `Day ${String(weekday + 1)}`

  return (
    <section
      aria-label={label}
      data-today={isToday ? 'true' : undefined}
      className={cx('min-w-0', isToday ? 'order-first lg:order-none' : '')}
    >
      <h2 className="px-2 pb-2.5 text-[12px] font-semibold tracking-[0.08em] uppercase">
        <span className={isToday ? 'text-[var(--arc-ember)]' : 'text-[var(--arc-text-muted)]'}>
          {label}
        </span>
        {isToday ? <span className="text-[var(--arc-ember)]">· today</span> : null}
      </h2>
      {entries.length === 0 ? (
        <p className="px-2 text-[13px] text-[var(--arc-text-muted)]">Nothing airing</p>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {entries.map((entry) => (
            <EntryRow key={entry.anime.id} entry={entry} />
          ))}
        </ul>
      )}
    </section>
  )
}

/**
 * Collapsed by default: films and OVAs have no slot, so they are a footnote to
 * a weekday grid rather than part of it.
 */
function Unscheduled({ entries }: { entries: ScheduleEntry[] }) {
  const [open, setOpen] = useState(false)
  if (entries.length === 0) return null

  return (
    <section className="mt-12">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((current) => !current)
        }}
        className={buttonClass('chip')}
      >
        {`${open ? 'Hide' : 'Show'} ${UNSCHEDULED_TITLE} (${String(entries.length)})`}
      </button>
      {open ? (
        <ul className="mt-4 grid gap-0.5 sm:grid-cols-2 lg:grid-cols-3">
          {entries.map((entry) => (
            <EntryRow key={entry.anime.id} entry={entry} statusClassName="px-2 pb-2" />
          ))}
        </ul>
      ) : null}
    </section>
  )
}

/**
 * "Times in Europe/Berlin", with a way to change it (spec §4.1 FR-C3).
 *
 * The zone belongs to the account, not to this page: the server groups the
 * grid into weekday columns in it, so saving one re-fetches the schedule
 * rather than re-formatting what is on screen. Until that answer lands the
 * header reads the zone off the user the save returned — the response in
 * flight still carries the old one, and a header that flicked back to it would
 * read as the save having failed.
 */
function TimezoneControl({ timezone }: { timezone: string }) {
  const [editing, setEditing] = useState(false)
  const [choice, setChoice] = useState(timezone)
  const update = useUpdateTimezone()

  const shown = update.data?.timezone ?? timezone
  const zones = useMemo(() => timezoneOptions(shown), [shown])

  function open() {
    update.reset()
    setChoice(shown)
    setEditing(true)
  }

  function cancel() {
    update.reset()
    setEditing(false)
  }

  function save() {
    update.mutate(choice, {
      onSuccess: () => {
        setEditing(false)
      },
    })
  }

  return (
    <div className="mt-1 text-[13px] text-[var(--arc-text-muted)]">
      <p>
        <span>{`Times in ${shown}`}</span>
        {editing ? null : (
          <button
            type="button"
            onClick={open}
            className={cx(
              'ml-2 rounded-sm underline underline-offset-2 hover:text-[var(--arc-text)]',
              FOCUS_RING,
            )}
          >
            Change
          </button>
        )}
      </p>

      {editing ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label htmlFor="schedule-timezone" className="text-[13px]">
            Timezone
          </label>
          <select
            id="schedule-timezone"
            value={choice}
            disabled={update.isPending}
            onChange={(event) => {
              setChoice(event.target.value)
            }}
            className={inputClass()}
          >
            {zones.map((zone) => (
              <option key={zone} value={zone}>
                {zone}
              </option>
            ))}
          </select>
          <button
            type="button"
            className={buttonClass('chip')}
            disabled={update.isPending}
            onClick={save}
          >
            Save
          </button>
          <button
            type="button"
            className={buttonClass('chip')}
            disabled={update.isPending}
            onClick={cancel}
          >
            Cancel
          </button>
        </div>
      ) : null}

      {update.isError ? (
        <p role="alert" className={`mt-2 ${FIELD_ERROR_CLASS}`}>
          {authErrorMessage(update.error)}
        </p>
      ) : null}
    </div>
  )
}

/**
 * How often "today" is re-read. A schedule is a tab people leave open, and one
 * left open past midnight would otherwise keep yesterday lit up until it was
 * reloaded. A minute is well inside the smallest zone offset (15 minutes) and
 * costs a `Date` read per tick.
 */
const TODAY_INTERVAL_MS = 60_000

/**
 * Which column is today, kept current while the page stays open. Returns -1 —
 * nothing highlighted — until the response says which zone to read the clock
 * in.
 */
function useToday(timezone: string | undefined): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const id = setInterval(() => {
      setNow(Date.now())
    }, TODAY_INTERVAL_MS)
    return () => {
      clearInterval(id)
    }
  }, [])

  return timezone === undefined ? -1 : weekdayInTimezone(timezone, new Date(now))
}

/**
 * Seasonal schedule (spec §4.1 FR-C3, §5, roadmap M4).
 *
 * The season lives in the URL (`?year=&season=`) so a particular season is a
 * shareable link and the back button steps through the ones already looked at;
 * a bare `/schedule` means "the current one", which only the server knows.
 */
export function Schedule() {
  const [searchParams, setSearchParams] = useSearchParams()
  const year = parseYear(searchParams.get('year'))
  const season = parseSeason(searchParams.get('season'))
  // The server takes both or neither; half a pair is treated as neither.
  const pinned = year !== undefined && season !== undefined
  const { data, error, isError, isFetching, refetch } = useSchedule(
    pinned ? year : undefined,
    pinned ? season : undefined,
  )

  function goTo(ref: SeasonRef) {
    setSearchParams({ year: String(ref.year), season: ref.season })
  }

  const today = useToday(data?.timezone)
  const isEmpty =
    data !== undefined &&
    data.unscheduled.length === 0 &&
    data.days.every((day) => day.entries.length === 0)

  return (
    <section className="mx-auto max-w-[110rem]">
      <div className="flex flex-wrap items-end justify-between gap-5">
        <div className="min-w-0">
          <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
            Schedule
          </h1>
          {data === undefined ? null : (
            <>
              <p className="mt-2 text-[14px] text-[var(--arc-text)]">
                {seasonLabel(data.year, data.season)}
              </p>
              <TimezoneControl timezone={data.timezone} />
            </>
          )}
        </div>

        <div className="flex items-center gap-2.5">
          <button
            type="button"
            className={buttonClass('chip')}
            disabled={data === undefined}
            onClick={() => {
              if (data !== undefined) goTo(data.prev)
            }}
          >
            Previous season
          </button>
          <button
            type="button"
            className={buttonClass('chip')}
            disabled={data === undefined}
            onClick={() => {
              if (data !== undefined) goTo(data.next)
            }}
          >
            Next season
          </button>
        </div>
      </div>

      {isError ? (
        <ErrorState
          className="mt-10"
          message={catalogErrorMessage(error, 'Could not load the schedule.')}
          pending={isFetching}
          onRetry={() => {
            void refetch()
          }}
        />
      ) : data === undefined ? (
        <Skeleton shape="row" count={5} className="mt-10 max-w-3xl" />
      ) : isEmpty ? (
        <EmptyState className="mt-10 max-w-3xl" message={EMPTY_SEASON} />
      ) : (
        <>
          {/* The week is laid to the design's 1180px content measure, which is
              what `Layout` caps every page at: seven columns and six 12px gaps
              make each day 158px — enough for a 38px thumb and a two-line
              title, which is what the column is for. Below that measure the
              week scrolls sideways inside this box rather than shrinking the
              columns to one word per line. Under `lg` there is no minimum and
              the days simply stack, today first. */}
          <div className="no-scrollbar mt-8 overflow-x-auto">
            <div
              className={cx(
                'grid grid-cols-1 gap-x-4 gap-y-8 md:grid-cols-2 lg:min-w-[1180px] lg:grid-cols-7 lg:gap-x-3',
                isFetching ? 'opacity-60' : '',
              )}
            >
              {data.days.map((day) => (
                <DayColumn
                  key={day.weekday}
                  weekday={day.weekday}
                  entries={day.entries}
                  isToday={day.weekday === today}
                />
              ))}
            </div>
          </div>
          <Unscheduled entries={data.unscheduled} />
        </>
      )}
    </section>
  )
}
