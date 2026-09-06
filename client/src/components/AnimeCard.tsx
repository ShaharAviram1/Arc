import { Link } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ListStatusControl } from '@/components/ListStatusControl'
import type { AnimeSummary } from '@/lib/anime'

/** "TV · 28 eps · 2023"; empty parts are dropped rather than left blank. */
function secondaryLine(anime: AnimeSummary): string {
  const parts: string[] = []
  if (anime.format !== null) parts.push(anime.format)
  if (anime.episodes !== null) parts.push(`${anime.episodes} eps`)
  if (anime.season_year !== null) parts.push(String(anime.season_year))
  return parts.join(' · ')
}

export function AnimeCard({ anime }: { anime: AnimeSummary }) {
  const secondary = secondaryLine(anime)

  return (
    <article className="flex flex-col overflow-hidden rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)]">
      <Link to={`/anime/${anime.id}`} className="block">
        <CoverThumb url={anime.cover_url} className="aspect-[2/3] w-full" />
      </Link>

      <div className="flex flex-1 flex-col gap-2 p-3">
        <Link
          to={`/anime/${anime.id}`}
          className="text-sm leading-snug font-medium text-[var(--arc-text)] hover:text-[var(--arc-accent)]"
        >
          {anime.title.preferred}
        </Link>
        {secondary === '' ? null : (
          <p className="text-xs text-[var(--arc-text-muted)]">{secondary}</p>
        )}
        {anime.source === 'mal' ? (
          <p>
            <span
              title="AniList is unavailable; this result came from MyAnimeList"
              className="inline-block rounded-full border border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10 px-1.5 py-0.5 text-[0.625rem] text-[var(--arc-warn)]"
            >
              via MAL
            </span>
          </p>
        ) : null}
        <ListStatusControl
          animeId={anime.id}
          status={anime.list_status}
          label={`List status for ${anime.title.preferred}`}
          className="mt-auto pt-1"
        />
      </div>
    </article>
  )
}
