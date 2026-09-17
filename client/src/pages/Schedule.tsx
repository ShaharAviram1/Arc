import { useEffect, useMemo, useState, type KeyboardEvent } from 'react'
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
  GLASS_CIRCLE,
} from '@/components/ui'
import { catalogErrorMessage } from '@/lib/anime'
import { authErrorMessage, timezoneOptions, useUpdateTimezone } from '@/lib/auth'
import {
  currentSeason,
  parseSeason,
  parseYear,
  seasonLabel,
  SEASONS,
  useSchedule,
  weekDates,
  weekdayInTimezone,
  WEEKDAY_CODES,
  WEEKDAY_LABELS,
  type ScheduleDay,
  type ScheduleEntry,
  type Season,
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
    <span className="mt-1 block text-[15px] tabular-nums text-[var(--arc-text-muted)]">
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
 * "Since Spring 2026" — where a show in this week's grid started, when that is
 * not the season the page is showing (`carried_over`, owner 2026-09-13).
 *
 * The current week's grid holds every show on air, whatever season it carries,
 * so a two-cour show that began in spring sits in the summer grid without
 * contradiction — but a person scanning a season page is entitled to know
 * which of these rows the season is actually *about*. One faint 12px line
 * inside the row's existing text column, below the slot: no new layout, and
 * quiet enough that the ordinary row (which never renders it) is unchanged.
 *
 * Nothing is rendered for a show the catalogue gives no season year: a
 * long-runner is carried into the week the same way, and "Since —" says less
 * than silence.
 */
function CarriedOverLine({ entry }: { entry: ScheduleEntry }) {
  const { season, season_year: year } = entry.anime
  if (!entry.carried_over || year === null) return null

  const label =
    season !== null && (SEASONS as readonly string[]).includes(season)
      ? seasonLabel(year, season as Season)
      : String(year)

  return (
    <span className="mt-0.5 block text-[12px] text-[var(--arc-text-faint)]">{`Since ${label}`}</span>
  )
}

/**
 * One show in a day column: a 56px key visual, the whole title, the slot and
 * the caveat where there is one.
 *
 * Three columns instead of seven (owner, 2026-09-17) is what pays for all of
 * this. At 158px a title broke one word per line, so it was clamped to two
 * lines at 13px and the time was 12px; at ~350px the title fits whole at 15px
 * and the time is legible at the size the rest of Arc reads at. Nothing is
 * clamped any more: "show the names" was the ask, and a row that grows by a
 * line is cheaper than a name the reader has to hover to finish.
 *
 * The list-status control still only appears below `lg` — the week is roomier,
 * not infinite, and the design's own answer for the wide grid is that the
 * control is one tap away on the show page.
 *
 * `aria-label` on the link so its accessible name stays the title alone; the
 * slot inside it is detail, not part of what the link is called.
 *
 * A followed show keeps the column's air-time order — the order is what a
 * person reads a schedule for (FR-C3) — and is marked by an ember left rule
 * over a 7% tint, with "On your list" for a screen reader. Quiet on purpose:
 * the owner asked for "a little highlight", and a badge on half the rows of a
 * personal schedule is noise. The rule is transparent rather than absent on an
 * ordinary row, so the two kinds of row still start at the same pixel.
 */
function EntryRow({
  entry,
  statusClassName = 'px-3 pb-3 lg:hidden',
}: {
  entry: ScheduleEntry
  /**
   * Where the list-status control is allowed to appear. The week grid hides it
   * from `lg` up; the unscheduled list, which is never a day column, keeps it
   * at every size.
   */
  statusClassName?: string
}) {
  const { anime } = entry
  const href = `/anime/${String(anime.id)}`

  return (
    <li
      data-following={entry.following ? 'true' : undefined}
      className={cx(
        'rounded-row border-l-2 transition-colors duration-200',
        entry.following
          ? 'border-[color-mix(in_srgb,var(--arc-ember)_55%,transparent)] bg-[color-mix(in_srgb,var(--arc-ember)_7%,transparent)]'
          : 'border-transparent hover:bg-[var(--arc-surface-hover)]',
      )}
    >
      {/* Outside the link on purpose: the link is named by its `aria-label`,
          so anything inside it is never read. */}
      {entry.following ? <span className="sr-only">On your list</span> : null}

      <Link
        to={href}
        aria-label={anime.title.preferred}
        title={anime.source === 'mal' ? VIA_MAL_HINT : undefined}
        className={cx('flex items-start gap-3.5 rounded-row p-3', FOCUS_RING)}
      >
        <span className="block w-[56px] shrink-0">
          <Artwork url={anime.cover_url} shape="thumb" />
        </span>
        <span className="block min-w-0 flex-1">
          {/* `break-words` is for the single long word a Japanese title
              transliterates into, which would otherwise run out of the column
              rather than wrap inside it. */}
          <span className="block text-[15px] leading-[1.35] font-medium break-words text-[var(--arc-text)]">
            {anime.title.preferred}
          </span>
          <SlotLine entry={entry} />
          <CarriedOverLine entry={entry} />
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
 * One day of the three on screen: "Wed 17 Sep" over its shows.
 *
 * The heading is both the column's title and its cell of the day-and-date bar
 * — one row of headings with the chevrons at its ends *is* the bar — so the
 * hairline under it runs the width of the column and turns ember on today.
 * Keeping it here rather than in a bar of its own is what makes the phone
 * layout work: the days stack, and each heading stacks with the shows it
 * belongs to instead of stranding three labels at the top of the page.
 *
 * The accessible name stays the plain weekday ("Wednesday"): the date is in
 * the heading, which a screen reader reads on the way in, and a landmark whose
 * name changes every midnight is a landmark nobody can refer to.
 */
function DayColumn({
  weekday,
  date,
  entries,
  isToday,
}: {
  weekday: number
  /**
   * "17 Sep" — undefined before the timezone is known, and on a browsed
   * season, which is a set of weekday slots rather than a week with dates.
   */
  date: string | undefined
  entries: ScheduleEntry[]
  isToday: boolean
}) {
  const label = WEEKDAY_LABELS[weekday] ?? `Day ${String(weekday + 1)}`
  const short = WEEKDAY_CODES[weekday] ?? label
  const heading = date === undefined ? label : `${short} ${date}`

  return (
    <section aria-label={label} data-today={isToday ? 'true' : undefined} className="min-w-0">
      <h2
        className={cx(
          'mb-4 flex flex-wrap items-baseline gap-2.5 border-b px-3 pb-2.5 text-[17px] font-semibold tracking-[-0.01em]',
          isToday
            ? 'border-[color-mix(in_srgb,var(--arc-ember)_60%,transparent)]'
            : 'border-[var(--arc-hairline)]',
        )}
      >
        <span className={isToday ? 'text-[var(--arc-ember)]' : 'text-[var(--arc-text)]'}>
          {heading}
        </span>
        {isToday ? (
          <span className="rounded-full border-[0.5px] border-[color-mix(in_srgb,var(--arc-ember)_45%,transparent)] px-2 py-0.5 text-[11px] font-semibold tracking-[0.08em] uppercase text-[var(--arc-ember)]">
            Today
          </span>
        ) : null}
      </h2>
      {entries.length === 0 ? (
        <p className="px-3 text-[14px] text-[var(--arc-text-muted)]">Nothing airing</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {entries.map((entry) => (
            <EntryRow key={entry.anime.id} entry={entry} />
          ))}
        </ul>
      )}
    </section>
  )
}

/** The chevrons at the ends of the day bar, in the hero's own glass circle. */
const CHEVRON = cx(
  GLASS_CIRCLE,
  FOCUS_RING,
  'mt-1 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-[var(--arc-surface-raised)]',
)

/** How many days are on screen at once (owner, 2026-09-17). */
const WINDOW_DAYS = 3

/** The last day the window can start on and still hold three days. */
const MAX_START = WEEKDAY_LABELS.length - WINDOW_DAYS

const DAY_BAR_LABEL = 'Days shown'

/**
 * Three days of the week the server sent, with arrows at both ends.
 *
 * The seven-column grid was cramped from the day the current week started
 * carrying every airing show, long-runners included (owner, 2026-09-17):
 * 158px per column is not a schedule, it is a list of abbreviations. Three
 * columns of the same 1180px measure are ~350px each, which is a whole title,
 * a legible time and a 56px thumb.
 *
 * The window never runs off either end of the week: the arrows stop at Monday
 * and at Friday-start, because the week is all the server sent and a fourth
 * column of nothing is not what the arrow promised. Stepping seasons is a
 * different axis and keeps its own pair of buttons.
 *
 * Left/right move the window whenever focus is anywhere in the group — except
 * in a field, where the arrows belong to the field: they change a `<select>`'s
 * value, and the list-status control is a `<select>`.
 */
function DayWindow({
  days,
  dates,
  today,
  start,
  onMove,
  dimmed,
}: {
  days: ScheduleDay[]
  /**
   * Week dates by weekday index; empty until the timezone is known and on a
   * browsed season, where the bar is weekday names alone.
   */
  dates: string[]
  /** The weekday to mark, or -1 for none — a browse has no today. */
  today: number
  start: number
  onMove: (delta: number) => void
  dimmed: boolean
}) {
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    const tag = (event.target as HTMLElement).tagName.toLowerCase()
    if (tag === 'select' || tag === 'input' || tag === 'textarea') return
    event.preventDefault()
    onMove(event.key === 'ArrowLeft' ? -1 : 1)
  }

  return (
    <div
      role="group"
      aria-label={DAY_BAR_LABEL}
      onKeyDown={onKeyDown}
      className="mt-8 flex items-start gap-3"
    >
      <button
        type="button"
        aria-label="Previous day"
        disabled={start <= 0}
        onClick={() => {
          onMove(-1)
        }}
        className={CHEVRON}
      >
        <span aria-hidden>‹</span>
      </button>

      <div
        className={cx(
          'grid min-w-0 flex-1 grid-cols-1 gap-x-6 gap-y-10 md:grid-cols-3',
          dimmed ? 'opacity-60' : '',
        )}
      >
        {days.slice(start, start + WINDOW_DAYS).map((day) => (
          <DayColumn
            key={day.weekday}
            weekday={day.weekday}
            date={dates[day.weekday]}
            entries={day.entries}
            isToday={day.weekday === today}
          />
        ))}
      </div>

      <button
        type="button"
        aria-label="Next day"
        disabled={start >= MAX_START}
        onClick={() => {
          onMove(1)
        }}
        className={CHEVRON}
      >
        <span aria-hidden>›</span>
      </button>
    </div>
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
            <EntryRow key={entry.anime.id} entry={entry} statusClassName="px-3 pb-3" />
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

/** Keeps the window inside the week the server sent. */
function clampStart(value: number): number {
  return Math.min(Math.max(value, 0), MAX_START)
}

/**
 * Which column is today, what the week's dates are and which season is the
 * live one — all kept current while the page stays open, the first two read in
 * the schedule's own zone and the third in UTC, where the server reads it.
 * `today` is -1 and `dates` empty until the response says which zone that is.
 *
 * One interval for the three: they answer the same question ("what day is it,
 * and where does that put the viewer"), and two would tick apart.
 */
function useClock(timezone: string | undefined): {
  today: number
  dates: string[]
  season: SeasonRef
} {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const id = setInterval(() => {
      setNow(Date.now())
    }, TODAY_INTERVAL_MS)
    return () => {
      clearInterval(id)
    }
  }, [])

  return useMemo(() => {
    const at = new Date(now)
    const season = currentSeason(at)
    if (timezone === undefined) return { today: -1, dates: [], season }
    return { today: weekdayInTimezone(timezone, at), dates: weekDates(timezone, at), season }
  }, [timezone, now])
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

  const { today, dates, season: liveSeason } = useClock(data?.timezone)

  /**
   * Whether the grid on screen is this week or a catalogue browse. Only the
   * live season's grid is a real Monday–Sunday week — it is the one the server
   * fills with every show on air — so only it gets dates on its bar and a day
   * marked today (owner, 2026-09-17). A browse is the shows of that season by
   * weekday, and printing this week's dates over them would say they air then.
   */
  const thisWeek =
    data !== undefined && data.year === liveSeason.year && data.season === liveSeason.season

  /**
   * How far the viewer has walked from today, not which day is on the left.
   * The zone arrives with the response, so today is -1 on the first render and
   * a start pinned then would be Monday for ever; an offset lets the window
   * land on today the moment the answer does, and follow it past midnight. A
   * browse has no today to start from, so it starts on Monday.
   */
  const [offset, setOffset] = useState(0)
  const anchor = thisWeek && today >= 0 ? today : 0
  const start = clampStart(anchor + offset)

  function move(delta: number) {
    setOffset(clampStart(start + delta) - anchor)
  }

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
          {/* Three columns of the design's 1180px measure — what `Layout` caps
              every page at, less the two chevrons — are ~350px each. Under
              `md` they stack in window order, today first, which is the
              phone's own reading of "what is on tonight". */}
          <DayWindow
            days={data.days}
            dates={thisWeek ? dates : []}
            today={thisWeek ? today : -1}
            start={start}
            onMove={move}
            dimmed={isFetching}
          />
          <Unscheduled entries={data.unscheduled} />
        </>
      )}
    </section>
  )
}
