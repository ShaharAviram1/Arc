import { Link } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ListStatusControl } from '@/components/ListStatusControl'
import { SourceBadge } from '@/components/SourceBadge'
import { summaryLine, type AnimeSummary } from '@/lib/anime'

export function AnimeCard({ anime }: { anime: AnimeSummary }) {
  const secondary = summaryLine(anime)

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
        <SourceBadge source={anime.source} />
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
