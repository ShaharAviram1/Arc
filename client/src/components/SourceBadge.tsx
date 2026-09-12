import type { CatalogSource } from '@/lib/anime'

/**
 * The caveat a record carries when a live catalogue has not filled it (spec
 * §4.1 FR-C6): "via MAL" when AniList has never answered, "via offline
 * catalogue" when neither live source has and the row came from the weekly
 * offline import (M15.5). Either way air dates and episode counts are
 * second-hand, so anywhere a show is shown as a card the caveat travels with
 * it — and disappears on its own once a live source fills the row, because the
 * server keeps `source` current and this reads nothing else.
 *
 * Nothing is rendered for an AniList record, which is the normal case and
 * needs no label.
 */
const CAVEATS = {
  mal: {
    text: 'via MAL',
    title: 'AniList is unavailable; this result came from MyAnimeList',
  },
  offline: {
    text: 'via offline catalogue',
    title: 'Live catalogues are unavailable; this result came from the weekly offline import',
  },
} as const

export function SourceBadge({ source }: { source: CatalogSource | null }) {
  if (source !== 'mal' && source !== 'offline') return null
  const caveat = CAVEATS[source]

  // A line of quiet text rather than a pill (M15): the design puts no badge on
  // or beside artwork, and a warning-coloured chip made a routine fallback
  // look like a fault. The caveat still travels with the card; it just stops
  // shouting.
  return (
    <p title={caveat.title} className="mt-1 text-[11px] text-[var(--arc-text-muted)]">
      {caveat.text}
    </p>
  )
}
