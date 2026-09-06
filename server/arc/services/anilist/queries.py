"""The GraphQL documents Arc sends to AniList.

Five of them, and they are deliberately the only five: everything the catalogue
needs is a search, a single title by AniList id, the same title by MAL id, one
more page of that title's back catalogue, or a whole season. Keeping the
documents in one module means the shape of what comes back is written down in
exactly one place (:mod:`arc.services.anilist.client` parses it).

``SUMMARY_FRAGMENT`` is the field set a search result carries — enough for a
result card and for the columns :mod:`arc.services.catalog.cache` calls
"summary". ``DETAIL_SELECTION`` adds everything else, including the airing
schedule that ``episodes`` rows are built from. ``MEDIA_BY_MAL_ID`` is the
detail query keyed on ``idMal`` instead of ``id``, which is what the
reconciliation job uses to attach an AniList id to a row that arrived through
MAL (FR-C6). ``AIRED_SCHEDULE_PAGE`` fetches page 2 and beyond of a schedule
for the handful of shows longer than one page, and ``SEASON`` lists a season
for the daily pre-cache (FR-C7).
"""

from __future__ import annotations

#: Fields shared by the search results, the season list and the by-id queries.
#: ``coverImage`` asks for ``extraLarge`` first: AniList's ``large`` is 230 px
#: wide, which is visibly soft on a show page, and ``extraLarge`` is what every
#: client here renders.
SUMMARY_FRAGMENT = """
fragment ArcSummary on Media {
  id
  idMal
  title { romaji english native }
  format
  episodes
  status
  season
  seasonYear
  coverImage { extraLarge large }
}
"""

#: Live title search (FR-C1). ``sort: SEARCH_MATCH`` is AniList's own
#: relevance order; anything else (POPULARITY, START_DATE) buries the title
#: the user actually typed under its more popular relatives.
SEARCH = (
    SUMMARY_FRAGMENT
    + """
query ArcSearch($search: String!, $page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { currentPage hasNextPage }
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) { ...ArcSummary }
  }
}
"""
)

#: Everything a detail fetch asks for beyond the summary.
#:
#: The airing schedule is asked for as two aliased connections rather than one
#: unfiltered page. ``airingSchedule`` has no sort argument and pages at 100,
#: so a single page of a long-running show (One Piece is past 1100 episodes)
#: would return its first hundred episodes and nothing about what airs on
#: Sunday — which is the half Arc actually needs. Splitting on ``notYetAired``
#: guarantees the upcoming episodes are present whatever the back catalogue
#: looks like.
#:
#: The ``aired`` half still pages at 100, so it asks for ``pageInfo`` too:
#: that is the flag :meth:`AniListClient.media` follows with
#: :data:`AIRED_SCHEDULE_PAGE` until the back catalogue is complete. Page 1
#: stays here so that the common case — every show shorter than 101 episodes —
#: is a single round trip.
#:
#: Written once and shared by the two detail documents, which differ only in
#: which id argument they take: a field that drifted between them would mean a
#: reconciled row quietly missing a column.
DETAIL_SELECTION = """
    ...ArcSummary
    bannerImage
    description
    genres
    synonyms
    tags { name rank }
    studios(isMain: true) { nodes { name } }
    nextAiringEpisode { episode airingAt timeUntilAiring }
    relations {
      edges {
        relationType
        node { id idMal type format title { romaji english native } }
      }
    }
    aired: airingSchedule(notYetAired: false, page: 1, perPage: 100) {
      pageInfo { currentPage hasNextPage }
      nodes { episode airingAt }
    }
    upcoming: airingSchedule(notYetAired: true, perPage: 100) {
      nodes { episode airingAt }
    }
"""

#: One title by AniList id, with everything the cache stores.
MEDIA_BY_ID = (
    SUMMARY_FRAGMENT
    + """
query ArcMedia($id: Int!) {
  Media(id: $id, type: ANIME) {"""
    + DETAIL_SELECTION
    + """  }
}
"""
)

#: The same title, keyed on its MyAnimeList id. This is the query that makes a
#: MAL-first row and an AniList row the same row (FR-C6): the reconciliation
#: job asks with the MAL id it already has and stores the AniList id that comes
#: back.
MEDIA_BY_MAL_ID = (
    SUMMARY_FRAGMENT
    + """
query ArcMediaByMal($idMal: Int!) {
  Media(idMal: $idMal, type: ANIME) {"""
    + DETAIL_SELECTION
    + """  }
}
"""
)

#: Page 2..N of the aired half of a title's schedule, and nothing else.
#:
#: Deliberately not the whole ``Media`` object again: a follow-up page is only
#: ever asked for on a long-running show, and re-fetching a kilobyte of
#: synopsis and tags per hundred episodes to throw them away would make One
#: Piece a twelve-fold heavier refresh than it needs to be.
AIRED_SCHEDULE_PAGE = """
query ArcAiredSchedule($id: Int!, $page: Int!) {
  Media(id: $id, type: ANIME) {
    id
    aired: airingSchedule(notYetAired: false, page: $page, perPage: 100) {
      pageInfo { currentPage hasNextPage }
      nodes { episode airingAt }
    }
  }
}
"""

#: One page of a season, most popular first (FR-C7). The daily pre-cache writes
#: these as summaries, so a season nobody has opened still renders when both
#: sources are down.
#:
#: ``nextAiringEpisode`` is the one field asked for beyond the summary
#: fragment, and it is what makes those rows a *schedule* rather than a list:
#: the weekday a show sits on comes from its next broadcast (FR-C3), and
#: without it the pre-cache would produce two hundred titles that no day of the
#: week claims. It is not in the fragment itself because a search result has no
#: use for it and would then have to be stopped from writing a null over a
#: cached one.
SEASON = (
    SUMMARY_FRAGMENT
    + """
query ArcSeason($season: MediaSeason!, $seasonYear: Int!, $page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { currentPage hasNextPage }
    media(season: $season, seasonYear: $seasonYear, type: ANIME, sort: POPULARITY_DESC) {
      ...ArcSummary
      nextAiringEpisode { episode airingAt }
    }
  }
}
"""
)

__all__ = [
    "AIRED_SCHEDULE_PAGE",
    "DETAIL_SELECTION",
    "MEDIA_BY_ID",
    "MEDIA_BY_MAL_ID",
    "SEARCH",
    "SEASON",
    "SUMMARY_FRAGMENT",
]
