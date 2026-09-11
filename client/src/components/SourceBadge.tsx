import type { CatalogSource } from '@/lib/anime'

/**
 * "via MAL" — the marker on a record AniList has never filled (spec §4.1
 * FR-C6). It is a caveat, not a feature: air dates and episode counts on such
 * a record are MAL's estimates until AniList comes back, so anywhere a show is
 * shown as a card the caveat travels with it.
 *
 * Nothing is rendered for an AniList record, which is the normal case and
 * needs no label.
 */
export function SourceBadge({ source }: { source: CatalogSource | null }) {
  if (source !== 'mal') return null

  // A line of quiet text rather than a pill (M15): the design puts no badge on
  // or beside artwork, and a warning-coloured chip made a routine fallback
  // look like a fault. The caveat still travels with the card; it just stops
  // shouting.
  return (
    <p
      title="AniList is unavailable; this result came from MyAnimeList"
      className="mt-1 text-[11px] text-[var(--arc-text-muted)]"
    >
      via MAL
    </p>
  )
}
