import { Link } from 'react-router-dom'
import { ListStatusControl } from '@/components/ListStatusControl'
import { SourceBadge } from '@/components/SourceBadge'
import { Artwork, cx, FOCUS_RING } from '@/components/ui'
import { keyVisual, summaryLine, type AnimeSummary } from '@/lib/anime'

/**
 * One show in the Browse grid (M15 design handoff, "Browse").
 *
 * Framed 2:3 key visual with the title *below* it, never burned over the art,
 * and no badge on the artwork at all — the "via MAL" caveat and the list
 * status are text and a control under the title, where they can be read and
 * operated rather than decoded. The whole card is not one target: the art and
 * the title go to the show, and the status control is its own thing, because
 * a select nested inside a link is a trap for both mouse and keyboard.
 */
export function AnimeCard({ anime }: { anime: AnimeSummary }) {
  const secondary = summaryLine(anime)
  const path = `/anime/${String(anime.id)}`

  return (
    <article className="flex min-w-0 flex-col">
      <Link to={path} className={cx('group block rounded-art', FOCUS_RING)}>
        <Artwork
          url={keyVisual(anime)}
          shape="key"
          className="shadow-grid transition-transform duration-[240ms] ease-arc group-hover:-translate-y-[5px]"
        />
        <span className="mt-2.5 block text-[15px] leading-snug font-medium text-[var(--arc-text)]">
          {anime.title.preferred}
        </span>
      </Link>

      {secondary === '' ? null : (
        <p className="mt-1 text-[13px] text-[var(--arc-text-muted)]">{secondary}</p>
      )}

      <SourceBadge source={anime.source} />

      <ListStatusControl
        animeId={anime.id}
        status={anime.list_status}
        label={`List status for ${anime.title.preferred}`}
        className="mt-2.5"
      />
    </article>
  )
}
