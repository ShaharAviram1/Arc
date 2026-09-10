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

  return (
    <p>
      <span
        title="AniList is unavailable; this result came from MyAnimeList"
        className="inline-block rounded-full border border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10 px-1.5 py-0.5 text-[0.625rem] text-[var(--arc-warn)]"
      >
        via MAL
      </span>
    </p>
  )
}
