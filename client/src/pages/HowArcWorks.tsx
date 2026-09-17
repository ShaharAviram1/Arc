import { Link } from 'react-router-dom'
import { cx, FOCUS_RING } from '@/components/ui'

/**
 * "How Arc works" (M16, owner 2026-09-18).
 *
 * Written for one reader: somebody who has never watched anime, has no
 * MyAnimeList account, and has ten minutes to work out what this is. So it
 * explains the *machine* rather than the medium — a list, a schedule, a
 * search, a download, a transcode, a player, a list again — and it names every
 * outside service Arc talks to, because "where does the video come from" is
 * the first question anybody asks and a page that ducks it is worse than no
 * page.
 *
 * The nav entry that leads here is on the demo account alone (`is_demo`,
 * `Layout.tsx`). The **route** is not gated: a link somebody sends on should
 * not 404, and there is nothing on this page that is not already in the
 * repository's own documentation.
 *
 * Every claim below is taken from spec.md and architecture.md rather than
 * written from memory, and the numbers that are configurable are described as
 * such rather than quoted — "the next few episodes" and not "two", because an
 * admin can change it and a page that disagrees with the setting is a page
 * that lies.
 */

const LEDE =
  'Arc is a personal anime server. You keep a list of the shows you are watching; Arc works out ' +
  'when their new episodes air, finds them, prepares them for the browser and plays them — and ' +
  'what you watch flows back to your public list, so the list stays true without you editing it.'

/* --- The pipeline ------------------------------------------------------ */

interface Step {
  name: string
  /** One line, in the language of the thing rather than of the code. */
  detail: string
}

/**
 * The eight things that happen between "I want to watch this" and "my list
 * says I did", in the order they happen. The same order as spec.md §6's
 * episode lifecycle, with the list at both ends because that is where a person
 * actually starts and finishes.
 */
const STEPS: Step[] = [
  { name: 'Your list', detail: 'You add a show and say you are watching it.' },
  { name: 'Schedule', detail: 'Arc learns when each episode airs, in your own timezone.' },
  { name: 'Search', detail: 'On air day it searches Nyaa for a release of that episode.' },
  { name: 'Download', detail: 'The best candidate is handed to qBittorrent, behind a VPN.' },
  { name: 'Match', detail: 'The finished file is matched to the episode it claims to be.' },
  { name: 'Prepare', detail: 'ffmpeg re-encodes it for the browser, subtitles burned in.' },
  { name: 'Watch', detail: 'You press play; the player remembers where you stopped.' },
  { name: 'Progress', detail: 'At 90% watched, your MyAnimeList list is updated.' },
]

const CARD =
  'flex h-full flex-col rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] px-3.5 py-3'

/**
 * The strip: eight numbered cards, left to right on a wide screen and top to
 * bottom on a narrow one, with an arrow between each pair.
 *
 * An ordered list, because the order *is* the content — a screen reader is
 * told "1 of 8" by the markup rather than by a drawn number, and the arrows
 * are decoration (`aria-hidden`) rather than the only thing saying which way
 * this runs. Two arrow glyphs, one per breakpoint, because a rightward arrow
 * above a stacked list points at the wall.
 *
 * A four-column grid rather than `overflow-x-auto`: eight cards on one line
 * would be 120px each at the page's own measure, which is narrower than the
 * words in them, and a page that scrolls sideways is the one thing the shell
 * forbids. A grid rather than a wrapping flex row so the two rows are the
 * same shape (orchestrator, 2026-09-18: five-and-three with the last three
 * stretched read as a mistake); the arrow at the end of a row is kept for
 * spacing but not drawn, because it would point at the margin.
 */
function Pipeline() {
  return (
    <ol
      aria-label="What Arc does, step by step"
      className="mt-6 flex list-none flex-col md:grid md:grid-cols-4 md:gap-y-2"
    >
      {STEPS.map((step, index) => (
        <li key={step.name} className="flex flex-col md:flex-row md:items-center">
          <div className={CARD}>
            <p className="text-[13px] tabular-nums text-[var(--arc-text-faint)]">
              Step {index + 1}
            </p>
            <p className="mt-0.5 text-[15px] leading-snug font-semibold text-[var(--arc-text)]">
              {step.name}
            </p>
            <p className="mt-1 text-[13px] leading-[1.45] text-[var(--arc-text-muted)]">
              {step.detail}
            </p>
          </div>
          {index === STEPS.length - 1 ? (
            // The last card keeps the arrow's width so it lines up with the
            // card above it; there is nothing for the arrow to point at.
            <span
              aria-hidden
              className="hidden shrink-0 px-1.5 text-[var(--arc-text-faint)] md:invisible md:inline"
            >
              →
            </span>
          ) : (
            <>
              <span aria-hidden className="self-center py-1 text-[var(--arc-text-faint)] md:hidden">
                ↓
              </span>
              <span
                aria-hidden
                className={cx(
                  'hidden shrink-0 px-1.5 text-[var(--arc-text-faint)] md:inline',
                  index % 4 === 3 && 'md:invisible',
                )}
              >
                →
              </span>
            </>
          )}
        </li>
      ))}
    </ol>
  )
}

/* --- What Arc talks to -------------------------------------------------- */

interface Service {
  name: string
  what: string
}

/**
 * One sentence each, and every one of them checked against spec.md and
 * architecture.md rather than remembered: the point of this section is that a
 * reviewer can see what leaves the machine and what comes back.
 */
const SERVICES: Service[] = [
  {
    name: 'AniList',
    what:
      'The catalogue: titles, episode counts, air dates, cover art, genres and how shows relate ' +
      'to one another — and the search behind the Browse page.',
  },
  {
    name: 'MyAnimeList',
    what:
      'Your list and your progress. Arc imports it once when you connect your account and then ' +
      'writes to it only when you do something — finish an episode, change a status, set a ' +
      'score — and records every write, with its previous value, so you can undo it. It is also ' +
      "Arc's read-only stand-in for the catalogue when AniList is down.",
  },
  {
    name: 'TMDB',
    what: 'Key art, the wide backdrops behind the hero, and the still beside each episode.',
  },
  {
    name: 'The offline catalogue',
    what:
      'A copy of the manami anime-offline-database and Fribb’s cross-id map, imported once a ' +
      'week, so searching for a show and knowing that two services mean the same show never ' +
      'depend on a live API.',
  },
  {
    name: 'Nyaa',
    what: 'Searched for releases of one episode at a time, and never asked for anything else.',
  },
  {
    name: 'qBittorrent',
    what:
      'The download client, running behind a VPN with seeding switched off. Arc tells it what to ' +
      'fetch and watches for the file to finish.',
  },
  {
    name: 'ffmpeg',
    what:
      'Re-encodes every finished file into the one format every browser can play (HLS), with the ' +
      'subtitles burned into the picture.',
  },
  {
    name: 'A language model',
    what:
      'Writes the short case under each recommendation, and suggests an answer when Arc cannot ' +
      'tell which episode a file is. Its suggestions are shown, never applied. Which model that ' +
      'is, is a setting: Gemini by default, OpenRouter or Anthropic instead.',
  },
]

/** The three rules a reviewer should be able to check the rest of the app against. */
const RULES: string[] = [
  'Only the next few unwatched episodes of a show you are watching are fetched — never a whole ' +
    'season, and never a show you have not asked for.',
  'When Arc is not confident that a file is the episode it claims to be, the file goes to a ' +
    'review queue for a person to confirm. It never guesses and plays it anyway.',
  'MyAnimeList is written to only as a result of something you did, every write is logged with ' +
    'the value it replaced, and no automatic event ever lowers your progress.',
]

/* --- Where to look ------------------------------------------------------ */

interface Place {
  to: string
  label: string
  lines: [string, string]
}

const PLACES: Place[] = [
  {
    to: '/',
    label: 'Watch Now',
    lines: [
      'The front page: what is half-watched, what is ready to start, what airs this week and what ' +
        'has piled up.',
      'The big picture at the top is a show from this season that Arc thinks is worth your time.',
    ],
  },
  {
    to: '/schedule',
    label: 'Schedule',
    lines: [
      'Three days at a time, in your own timezone, with the shows on your list marked.',
      'Anime is broadcast weekly, so this is the calendar the whole server runs on.',
    ],
  },
  {
    to: '/list',
    label: 'A show page',
    lines: [
      'Open any show from My List: its episodes, one row each, and what Arc is doing about them.',
      'A row says wanted, searching, downloading, preparing or ready — the pipeline above, per ' +
        'episode.',
    ],
  },
  {
    to: '/',
    label: 'The player',
    lines: [
      'Press play on any ready episode from Watch Now.',
      'It starts on its own, resumes where you left off, and marks the episode watched at 90%.',
    ],
  },
]

/* --- Page --------------------------------------------------------------- */

const HEADING = 'text-[24px] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]'

export function HowArcWorks() {
  return (
    <section className="mx-auto max-w-5xl">
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        How Arc works
      </h1>
      <p className="mt-3 max-w-[66ch] text-[16px] leading-[1.6] text-[var(--arc-text-muted)]">
        {LEDE}
      </p>

      <section className="mt-12" aria-labelledby="how-pipeline">
        <h2 id="how-pipeline" className={HEADING}>
          From your list to your list
        </h2>
        <p className="mt-2 max-w-[66ch] text-[15px] leading-[1.6] text-[var(--arc-text-muted)]">
          Everything below happens before you press play, and nothing in it needs you.
        </p>
        <Pipeline />
      </section>

      <section className="mt-12" aria-labelledby="how-services">
        <h2 id="how-services" className={HEADING}>
          What Arc talks to
        </h2>
        <dl aria-label="The services Arc talks to" className="mt-5 flex flex-col gap-4">
          {SERVICES.map((service) => (
            <div key={service.name} className="max-w-[78ch]">
              <dt className="text-[15px] font-semibold text-[var(--arc-text)]">{service.name}</dt>
              <dd className="mt-0.5 text-[15px] leading-[1.6] text-[var(--arc-text-muted)]">
                {service.what}
              </dd>
            </div>
          ))}
        </dl>

        <h3 className="mt-8 text-[15px] font-semibold text-[var(--arc-text)]">
          Three rules it does not break
        </h3>
        <ul className="mt-2 flex max-w-[78ch] list-disc flex-col gap-2 pl-5 text-[15px] leading-[1.6] text-[var(--arc-text-muted)]">
          {RULES.map((rule) => (
            <li key={rule.slice(0, 24)}>{rule}</li>
          ))}
        </ul>
      </section>

      <section className="mt-12" aria-labelledby="how-places">
        <h2 id="how-places" className={HEADING}>
          Where to look
        </h2>
        <ul className="mt-5 flex flex-col gap-4">
          {PLACES.map((place) => (
            <li key={place.label} className="max-w-[78ch]">
              <Link
                to={place.to}
                className={cx(
                  'rounded-nav text-[15px] font-semibold text-[var(--arc-text)] underline decoration-[var(--arc-border-strong)] decoration-1 underline-offset-4 hover:decoration-[var(--arc-text)]',
                  FOCUS_RING,
                )}
              >
                {place.label}
              </Link>
              <p className="mt-1 text-[15px] leading-[1.6] text-[var(--arc-text-muted)]">
                {place.lines[0]}
              </p>
              <p className="text-[15px] leading-[1.6] text-[var(--arc-text-muted)]">
                {place.lines[1]}
              </p>
            </li>
          ))}
        </ul>
      </section>
    </section>
  )
}
