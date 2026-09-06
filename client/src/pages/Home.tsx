import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ListStatusControl } from '@/components/ListStatusControl'
import {
  catalogErrorMessage,
  episodeProgressPercent,
  episodeStateClass,
  episodeStateLabel,
  formatAirDate,
} from '@/lib/anime'
import { useMe } from '@/lib/auth'
import { useHealth } from '@/lib/health'
import { useHome, type BehindEntry, type NewEpisodeEntry } from '@/lib/schedule'

/** Why a date carries "est." — matches the show page's wording (FR-C6). */
const ESTIMATED_HINT = 'Estimated from the broadcast slot'

const CONTINUE_PLACEHOLDER = 'Nothing yet — playback arrives in a later milestone.'
const BEHIND_EMPTY = 'Nothing to catch up on — every followed show is up to date.'
const NEW_EMPTY = 'No episodes aired in the last seven days for the shows you follow.'

function HealthBadge() {
  const { data, isPending, isError } = useHealth()

  if (isPending) {
    return <span className="text-[var(--arc-text-muted)]">API: checking…</span>
  }
  if (isError || !data) {
    return <span className="text-[var(--arc-error)]">API: unreachable</span>
  }
  return (
    <>
      <span className="text-[var(--arc-ok)]">API: {data.status}</span>
      <span className="ml-2 text-[var(--arc-text-muted)]">
        {data.version} · {data.env}
      </span>
    </>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-8 first:mt-6">
      <h2 className="text-lg font-semibold tracking-tight text-[var(--arc-text)]">{title}</h2>
      {children}
    </section>
  )
}

function Note({ children }: { children: string }) {
  return <p className="mt-2 text-sm text-[var(--arc-text-muted)]">{children}</p>
}

/**
 * "Behind by 4 · 7 of 12 aired": how far behind, out of how much there is to
 * be behind on (FR-C4). A show still airing without a known total keeps the
 * shape rather than dropping the clause.
 */
function behindLine(item: BehindEntry): string {
  const total = item.anime.episodes === null ? '?' : String(item.anime.episodes)
  return `Behind by ${String(item.behind)} · ${String(item.aired)} of ${total} aired`
}

function BehindCard({ item, timezone }: { item: BehindEntry; timezone?: string }) {
  const { anime } = item

  return (
    <article className="flex gap-3 rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-3">
      <Link to={`/anime/${anime.id}`} className="block">
        <CoverThumb url={anime.cover_url} className="h-24 w-16 rounded" />
      </Link>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <Link
          to={`/anime/${anime.id}`}
          className="text-sm leading-snug font-medium text-[var(--arc-text)] hover:text-[var(--arc-accent)]"
        >
          {anime.title.preferred}
        </Link>
        <p className="text-xs text-[var(--arc-text-muted)] tabular-nums">{behindLine(item)}</p>
        <p className="text-xs text-[var(--arc-text-muted)]">
          {`Latest episode ${formatAirDate(item.latest_aired_at, timezone)}`}
        </p>
        <ListStatusControl
          animeId={anime.id}
          status={item.entry.status}
          label={`List status for ${anime.title.preferred}`}
          className="mt-auto pt-1"
        />
      </div>
    </article>
  )
}

function NewEpisodeRow({ item, timezone }: { item: NewEpisodeEntry; timezone?: string }) {
  const { anime, episode } = item
  // No bar here — the row is one line, so the number alone carries it (FR-A7).
  const percent = episodeProgressPercent(episode)

  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-[var(--arc-border)] px-3 py-2 first:border-t-0">
      <Link
        to={`/anime/${anime.id}`}
        className="min-w-0 flex-1 text-sm text-[var(--arc-text)] hover:text-[var(--arc-accent)]"
      >
        {anime.title.preferred}
      </Link>
      <span className="text-sm text-[var(--arc-text-muted)] tabular-nums">
        {`Episode ${String(episode.number)}`}
      </span>
      <span className="text-xs whitespace-nowrap text-[var(--arc-text-muted)]">
        {formatAirDate(episode.air_at, timezone)}
        {episode.air_at_estimated ? (
          <span title={ESTIMATED_HINT} className="ml-1.5 italic">
            est.
          </span>
        ) : null}
      </span>
      <span
        className={`inline-block rounded-full border px-2 py-0.5 text-xs ${episodeStateClass(episode.state)}`}
      >
        {episodeStateLabel(episode.state)}
      </span>
      {percent === null ? null : (
        <span className="text-xs text-[var(--arc-text-muted)] tabular-nums">
          {`${String(percent)}%`}
        </span>
      )}
    </li>
  )
}

/**
 * Home (spec §4.6 FR-W1, §5, roadmap M4): what the viewer was watching, what
 * they have fallen behind on, and what turned up this week. Continue watching
 * stays a placeholder until playback lands (roadmap M8).
 */
export function Home() {
  const { data: me } = useMe()
  const { data, error, isError } = useHome()
  const timezone = me?.timezone

  return (
    <section className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">Home</h1>

      {isError ? (
        <p role="alert" className="mt-6 text-sm text-[var(--arc-error)]">
          {catalogErrorMessage(error, 'Could not load your home page.')}
        </p>
      ) : data === undefined ? (
        <p role="status" className="mt-6 text-sm text-[var(--arc-text-muted)]">
          Loading…
        </p>
      ) : (
        <>
          <Section title="Continue watching">
            <Note>
              {data.continue_watching.length === 0
                ? CONTINUE_PLACEHOLDER
                : `${String(data.continue_watching.length)} episodes in progress.`}
            </Note>
          </Section>

          <Section title="Behind on">
            {data.behind.length === 0 ? (
              <Note>{BEHIND_EMPTY}</Note>
            ) : (
              <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {data.behind.map((item) => (
                  <BehindCard key={item.anime.id} item={item} timezone={timezone} />
                ))}
              </div>
            )}
          </Section>

          <Section title="New this week">
            {data.new_this_week.length === 0 ? (
              <Note>{NEW_EMPTY}</Note>
            ) : (
              <ul className="mt-3 overflow-hidden rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)]">
                {data.new_this_week.map((item) => (
                  <NewEpisodeRow
                    key={`${String(item.anime.id)}:${String(item.episode.id)}`}
                    item={item}
                    timezone={timezone}
                  />
                ))}
              </ul>
            )}
          </Section>
        </>
      )}

      <p className="mt-10 text-xs text-[var(--arc-text-muted)]">
        <HealthBadge />
      </p>
    </section>
  )
}
