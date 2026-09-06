import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ListStatusControl } from '@/components/ListStatusControl'
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

const buttonClass =
  'rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-1.5 text-sm text-[var(--arc-text)] transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-40'

/** Why a time carries "est." — same wording as the show page (FR-C6). */
const ESTIMATED_HINT = 'Estimated from the broadcast slot'

/**
 * "20:00 est. · Ep 7", with the parts the server actually knows. The marker
 * sits against the time because that is what it qualifies: the episode number
 * is not a guess even when the moment it airs is.
 */
function SlotLine({ entry }: { entry: ScheduleEntry }) {
  return (
    <p className="mt-0.5 text-xs text-[var(--arc-text-muted)] tabular-nums">
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
    </p>
  )
}

/**
 * One show in a day column. A followed show gets an accent edge rather than a
 * different position: the column stays in air-time order, which is what a
 * person reads it for (FR-C3).
 */
function EntryRow({ entry }: { entry: ScheduleEntry }) {
  const { anime } = entry
  const edge = entry.following
    ? 'border-l-[var(--arc-accent)] bg-[var(--arc-accent)]/5'
    : 'border-l-transparent'

  return (
    <li
      data-following={entry.following ? 'true' : undefined}
      className={`flex gap-2 rounded-md border-l-2 bg-[var(--arc-surface)] p-2 ${edge}`}
    >
      <CoverThumb url={anime.cover_url} className="h-14 w-10 rounded" />
      <div className="min-w-0 flex-1">
        <Link
          to={`/anime/${anime.id}`}
          className="block text-sm leading-snug font-medium text-[var(--arc-text)] hover:text-[var(--arc-accent)]"
        >
          {anime.title.preferred}
        </Link>
        <SlotLine entry={entry} />
        {anime.source === 'mal' ? (
          <p className="mt-1">
            <span
              title="AniList is unavailable; this row came from MyAnimeList"
              className="inline-block rounded-full border border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10 px-1.5 py-0.5 text-[0.625rem] text-[var(--arc-warn)]"
            >
              via MAL
            </span>
          </p>
        ) : null}
        <ListStatusControl
          animeId={anime.id}
          status={entry.list_status}
          label={`List status for ${anime.title.preferred}`}
          className="mt-1.5"
        />
      </div>
    </li>
  )
}

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
      className={`min-w-0 rounded-lg border p-2 ${
        isToday
          ? 'border-[var(--arc-accent)] bg-[var(--arc-accent)]/5'
          : 'border-[var(--arc-border)] bg-[var(--arc-surface)]/40'
      }`}
    >
      <h2 className="px-1 pb-2 text-xs font-medium tracking-wide text-[var(--arc-text-muted)] uppercase">
        {label}
        {isToday ? <span className="ml-1 text-[var(--arc-accent)]">· today</span> : null}
      </h2>
      {entries.length === 0 ? (
        <p className="px-1 pb-1 text-xs text-[var(--arc-text-muted)]">Nothing airing</p>
      ) : (
        <ul className="flex flex-col gap-2">
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
    <section className="mt-8">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((current) => !current)
        }}
        className={buttonClass}
      >
        {`${open ? 'Hide' : 'Show'} ${UNSCHEDULED_TITLE} (${String(entries.length)})`}
      </button>
      {open ? (
        <ul className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {entries.map((entry) => (
            <EntryRow key={entry.anime.id} entry={entry} />
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
    <div className="mt-0.5 text-xs text-[var(--arc-text-muted)]">
      <p>
        <span>{`Times in ${shown}`}</span>
        {editing ? null : (
          <button
            type="button"
            onClick={open}
            className="ml-2 rounded-sm underline underline-offset-2 hover:text-[var(--arc-text)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)]"
          >
            Change
          </button>
        )}
      </p>

      {editing ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <label htmlFor="schedule-timezone">Timezone</label>
          <select
            id="schedule-timezone"
            value={choice}
            disabled={update.isPending}
            onChange={(event) => {
              setChoice(event.target.value)
            }}
            className="rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-2 py-1.5 text-sm text-[var(--arc-text)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)] disabled:opacity-60"
          >
            {zones.map((zone) => (
              <option key={zone} value={zone}>
                {zone}
              </option>
            ))}
          </select>
          <button type="button" className={buttonClass} disabled={update.isPending} onClick={save}>
            Save
          </button>
          <button
            type="button"
            className={buttonClass}
            disabled={update.isPending}
            onClick={cancel}
          >
            Cancel
          </button>
        </div>
      ) : null}

      {update.isError ? (
        <p role="alert" className="mt-1 text-[var(--arc-error)]">
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
  const { data, error, isError, isFetching } = useSchedule(
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
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">Schedule</h1>
          {data === undefined ? null : (
            <>
              <p className="mt-1 text-sm text-[var(--arc-text)]">
                {seasonLabel(data.year, data.season)}
              </p>
              <TimezoneControl timezone={data.timezone} />
            </>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            className={buttonClass}
            disabled={data === undefined}
            onClick={() => {
              if (data !== undefined) goTo(data.prev)
            }}
          >
            Previous season
          </button>
          <button
            type="button"
            className={buttonClass}
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
        <p role="alert" className="mt-8 text-sm text-[var(--arc-error)]">
          {catalogErrorMessage(error, 'Could not load the schedule.')}
        </p>
      ) : data === undefined ? (
        <p role="status" className="mt-8 text-sm text-[var(--arc-text-muted)]">
          Loading…
        </p>
      ) : isEmpty ? (
        <p className="mt-8 text-sm text-[var(--arc-text-muted)]">{EMPTY_SEASON}</p>
      ) : (
        <>
          <div
            className={`mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-7 ${
              isFetching ? 'opacity-60' : ''
            }`}
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
          <Unscheduled entries={data.unscheduled} />
        </>
      )}
    </section>
  )
}
