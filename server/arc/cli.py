"""Operator commands: ``python -m arc.cli <command>`` (roadmap M11, M16).

Five things an operator has to do on a host with no browser session yet, and
one thing they always want to know:

* ``invite``            — issue an invite link and print it.
* ``warm-catalogue``    — queue the work that gives a fresh deployment a
                          schedule and covers, instead of waiting for 03:30 UTC.
* ``import-catalogue``  — import the offline catalogue **now**, in the
                          foreground, instead of waiting for Monday (M15.5).
* ``demo-list``         — put shows on somebody's list, by title, in one
                          status; optionally flag the account as the demo one.
* ``recs``              — produce one recommendation run for an account, so
                          its Recommendations page and Home's "Picked for you"
                          shelf have something on them (M16).
* ``status``            — a one-screen summary of the deployment.

Every one is **idempotent**: ``demo-list`` twice leaves one entry per title
and one ``is_demo`` flag, the two enqueueing commands deduplicate on the job
type, ``import-catalogue`` replaces what it imported last time (and skips the
work entirely when the files have not changed), and ``status`` writes nothing.
``recs`` is the exception and says so below — a run is an event, and a second
invocation is a second run against the daily budget (FR-R5).

These are thin wrappers over the same services the API calls — nothing here
reimplements a rule. ``demo-list`` in particular goes through
:func:`~arc.services.catalog.lists.set_list_entry`, so it recomputes the
acquisition window, stamps FR-A9's activation and respects the MAL write rules
exactly as the endpoint does (FR-C2, FR-W2, FR-M7); ``recs`` calls the same
:func:`~arc.services.recs.run_recommendations` as ``POST /api/recs/runs``.

Run it from ``server/``::

    uv run python -m arc.cli status
    uv run python -m arc.cli invite --email prof@example.edu
    uv run python -m arc.cli demo-list --user-email prof@example.edu --demo \\
        --add "Sousou no Frieren" --add "Vinland Saga"
    uv run python -m arc.cli demo-list --user-email prof@example.edu \\
        --status completed --progress 12 --add "Bocchi the Rock!"
    uv run python -m arc.cli recs --user-email prof@example.edu
    uv run python -m arc.cli warm-catalogue
    uv run python -m arc.cli import-catalogue

In production the same commands run inside the api container, which already
has the right ``DATABASE_URL``::

    docker compose --env-file .env -f deploy/docker-compose.yml \\
        run --rm api python -m arc.cli status
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings, get_settings
from arc.core.logging import setup_logging
from arc.db import create_engine, create_session_factory
from arc.models import (
    DEFAULT_PRIORITY,
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    MalLink,
    User,
    UserRole,
)
from arc.services.acquisition.rules import is_paused
from arc.services.auth import DEFAULT_EXPIRY_HOURS, create_invite, get_by_email, normalize_email
from arc.services.catalog import (
    ListEntryError,
    SourceUnavailable,
    preferred_title,
    set_list_entry,
)
from arc.services.catalog.cache import upsert_summaries
from arc.services.catalog.factory import catalog_for
from arc.services.catalog.names import CATALOG_PRIORITY, REFRESH_ALL, SEASON_SWEEP
from arc.services.catalog.offline.jobs import import_all
from arc.services.jobs import enqueue
from arc.services.recs import (
    DAILY_LIMIT,
    RecsEmptyPool,
    RecsFailed,
    RecsRateLimited,
    RecsRefused,
    RecsUnavailable,
    model_for,
    remaining_today,
    run_recommendations,
)
from arc.services.retention.sweep import retained_bytes

#: Exit code for "the command could not do what was asked" — no such user, no
#: search hit. Distinct from 2, which argparse uses for a bad command line.
EXIT_FAILED = 1


# --- invite -----------------------------------------------------------------


async def cmd_invite(session: AsyncSession, settings: Settings, args: argparse.Namespace) -> int:
    """Issue an invite link for ``--email`` and print it.

    Seven days by default, which is what architecture.md §7 specifies and what
    the API's own default is; ``--expires-in-hours`` overrides it up to 30 days.

    ``--admin`` is "make sure this address ends up an admin". Invites carry no
    role — :func:`~arc.services.auth.invites.accept` always creates a ``user``
    (and adding a role column to ``invites`` is a schema change, not a CLI
    flag) — so the flag promotes an account that already exists and, when one
    does not, says plainly that the invite must be accepted first. Both halves
    are idempotent: promoting an admin is a no-op.
    """
    email = normalize_email(args.email)

    if args.admin:
        existing = await get_by_email(session, email)
        if existing is not None:
            if existing.role is UserRole.ADMIN:
                print(f"{email} is already an admin (user {existing.id}); nothing to do.")
            else:
                existing.role = UserRole.ADMIN
                await session.commit()
                print(f"{email} promoted to admin (user {existing.id}).")
            return 0

    created = await create_invite(
        session,
        created_by=None,
        email=email,
        expires_in_hours=args.expires_in_hours,
    )
    await session.commit()

    url = f"{settings.public_url.rstrip('/')}/invite/{created.token}"
    print(url)
    print(f"  for      {email}", file=sys.stderr)
    print(f"  expires  {created.invite.expires_at.isoformat()}", file=sys.stderr)
    print(
        "  the link is shown once; Arc stores only its hash, so a lost link "
        "means issuing a new invite.",
        file=sys.stderr,
    )
    if args.admin:
        print(
            f"  --admin: no account for {email} yet. Invites always create a "
            "'user'; re-run this command with --admin once the invite has been "
            "accepted to promote it.",
            file=sys.stderr,
        )
    return 0


# --- warm-catalogue ---------------------------------------------------------


async def cmd_warm_catalogue(
    session: AsyncSession, settings: Settings, args: argparse.Namespace
) -> int:
    """Queue the season pre-cache and a refresh of everything anybody follows.

    Two jobs, both the worker's own (arc/worker.py), so the pacing, the
    spacing between children and the dedupe are the production ones rather
    than a second implementation:

    * ``catalog_season_sweep`` caches this season and the next, which is what
      the schedule page renders from (FR-C7). Without it a fresh deployment
      shows seven empty days until 03:30 UTC.
    * ``catalog_refresh_all`` fans out one spaced ``catalog_refresh`` per show
      that somebody is watching, has planned, or has on hold (FR-C5) — which
      on a fresh deployment is every show on every list. That is what fills in
      episode counts and covers.

    Both deduplicate on their type, so running this twice, or running it while
    the nightly sweep is pending, queues nothing new.
    """
    followed = await session.scalar(select(func.count(func.distinct(ListEntry.anime_id))))

    season = await enqueue(
        session, SEASON_SWEEP, priority=CATALOG_PRIORITY, dedupe_key=SEASON_SWEEP
    )
    refresh = await enqueue(session, REFRESH_ALL, priority=DEFAULT_PRIORITY, dedupe_key=REFRESH_ALL)
    await session.commit()

    print(f"season sweep    job {season.id} ({season.status.value})")
    print(f"refresh sweep   job {refresh.id} ({refresh.status.value})")
    print(f"listed shows    {followed or 0} (the refresh sweep fans out one job per followed show)")
    print("Both jobs deduplicate: an existing pending job is reported rather than doubled.")
    return 0


# --- import-catalogue -------------------------------------------------------


async def cmd_import_catalogue(
    session: AsyncSession, settings: Settings, args: argparse.Namespace
) -> int:
    """Download and import the offline catalogue now, in the foreground.

    The same :func:`~arc.services.catalog.offline.jobs.import_all` the Monday
    job runs, not a second implementation — so what an operator gets here is
    exactly what the scheduler would have produced, including the replace
    semantics and the "nothing changed" short-circuit.

    Inline rather than enqueued, unlike ``warm-catalogue``, and that is the
    point of having it: a fresh deployment wants the offline catalogue *before*
    somebody searches for something, and a queued job gives no way to watch it
    or to find out that the download 404ed. About half a minute: 6 MB and
    7.5 MB down, and 41.5k + 32k rows in (measured); a couple of seconds when
    neither file has changed.

    Exits 1 when **either** source failed, which is stricter than the job (it
    only fails when both did). An operator who ran this on purpose wants a
    non-zero exit for a half-import; the scheduler wants the half that worked
    to stand.
    """
    results, failures = await import_all(session, settings)

    for result in results:
        state = "unchanged" if result.unchanged else "imported"
        version = result.version or "(unknown version)"
        print(f"{result.source:<8} {state:<10} {version:<24} {result.rows} rows")
    for failure in failures:
        print(f"{failure.source:<8} FAILED     {failure.error}", file=sys.stderr)

    if failures:
        print(
            "The tables of any source that failed are untouched; "
            "the previous import is still in force.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return 0


# --- demo-list --------------------------------------------------------------


async def cmd_demo_list(session: AsyncSession, settings: Settings, args: argparse.Namespace) -> int:
    """Add shows to a user's list in one status, looked up by title.

    The point is a fresh deployment where the person who is about to log in
    has an empty Home page and an empty schedule. One command puts real shows
    on their list, which is what Home, the schedule highlights and acquisition
    all key off.

    One invocation seeds **one status group** (``--status``, default
    ``watching``) with **one progress** (``--progress``, optional), because
    that is the shape of a plausible list: several shows finished at their
    episode count, several part-way through, a couple planned. Four or five
    invocations make a list a person recognises; one flag per title would make
    a command line nobody can read (owner, 2026-09-18, for the M16 demo
    account).

    ``--demo`` sets ``users.is_demo`` on that account, which is what turns on
    the "How Arc works" entry in its nav and the strip on its Watch Now. It is
    a separate flag rather than implied by the command because seeding a list
    and nominating the demo account are two decisions, and the owner seeds their
    own list with this too.

    The lookup is the catalogue's own search, so it needs AniList (or MAL) to
    be reachable; the top hit is taken and printed, and a title that matches
    nothing is reported rather than guessed at. Adding the same show twice is
    a no-op beyond a touched ``updated_at``, because ``set_list_entry``
    describes a state rather than an event — and it is ``set_list_entry`` that
    does all of it: the acquisition window, FR-A9's ``activated_at`` stamp (so
    a seeded entry is active and actually fetches, unlike an imported one) and
    the FR-M4 write rules. The demo account has no MyAnimeList link, so no
    write is queued for it at all; that is asserted in ``tests/test_cli.py``
    rather than trusted.
    """
    email = normalize_email(args.user_email)
    user = await get_by_email(session, email)
    if user is None:
        print(f"no user with the address {email}", file=sys.stderr)
        return EXIT_FAILED

    status = ListStatus(args.status)

    if args.demo:
        # Idempotent, and reported either way: an operator re-running the
        # seeding script wants to see that the flag is on, not to wonder
        # whether the second run took it off.
        if user.is_demo:
            print(f"= {email} is already the demo account (user {user.id})")
        else:
            user.is_demo = True
            await session.commit()
            print(f"+ {email} flagged as the demo account (user {user.id})")

    failures = 0
    async with catalog_for(settings) as catalog:
        for title in args.add:
            try:
                page = await catalog.search(title, page=1)
            except SourceUnavailable as exc:
                print(f"! {title!r}: the catalogue is unavailable ({exc})", file=sys.stderr)
                failures += 1
                continue

            if not page.results:
                print(f"! {title!r}: no match in the catalogue", file=sys.stderr)
                failures += 1
                continue

            # Same upsert the search endpoint does: it is what mints the
            # internal id that the list entry is keyed on (FR-C6).
            rows = await upsert_summaries(session, page.results[:1])
            anime = rows[0]
            try:
                entry, anime = await set_list_entry(
                    session,
                    catalog,
                    user_id=user.id,
                    anime_id=anime.id,
                    status=status,
                    progress=args.progress,
                )
            except ListEntryError as exc:
                # The same rule the endpoint's 422 reports, from the same
                # function. Rolled back so one refused title does not leave a
                # half-written entry behind for the next one.
                await session.rollback()
                print(f"! {title!r}: {exc}", file=sys.stderr)
                failures += 1
                continue
            await session.commit()
            print(f"+ {preferred_title(anime)}  (anime {anime.id}) → {_entry_line(entry, anime)}")
            # Not refused — the endpoint accepts it too, and a stale episode
            # count is the likelier of the two explanations — but said out
            # loud, because a seeded list that reads "14 / 12" is the kind of
            # thing nobody notices until it is on a projector.
            if anime.episodes and entry.progress > int(anime.episodes):
                print(
                    f"  note: progress {entry.progress} is past the catalogue's "
                    f"{int(anime.episodes)} episodes for this show",
                    file=sys.stderr,
                )

    return EXIT_FAILED if failures else 0


def _entry_line(entry: ListEntry, anime: Anime) -> str:
    """``completed · 12 / 12`` — what the entry now says, for the operator."""
    if entry.progress == 0:
        return entry.status.value
    total = str(int(anime.episodes)) if anime.episodes else "?"
    return f"{entry.status.value} · {entry.progress} / {total}"


# --- recs -------------------------------------------------------------------


async def cmd_recs(session: AsyncSession, settings: Settings, args: argparse.Namespace) -> int:
    """Produce one recommendation run for a user (FR-R1…FR-R5, M16).

    The same call ``POST /api/recs/runs`` makes, with the same service, the
    same provider chain (``RECS_PROVIDER``) and the same daily budget — so the
    demo account's Recommendations page and Home's "Picked for you" shelf have
    content without anybody signing in as it and pressing the button.

    The one command here that is **not** idempotent, deliberately: a run is an
    event, FR-R5 stores it so the page is instant, and a second invocation
    spends a second of the ten runs a user gets per day. That is what the
    refresh button does too.

    Every way it can fail is one of the five the router turns into a status
    code (``arc/api/recs.py``); here they are one line on stderr and exit 1,
    because there is nobody to branch on a status code.
    """
    email = normalize_email(args.user_email)
    user = await get_by_email(session, email)
    if user is None:
        print(f"no user with the address {email}", file=sys.stderr)
        return EXIT_FAILED

    async with model_for(settings) as model:
        if model is None:
            print(
                "no recommendation provider is configured; set the API key for "
                f"RECS_PROVIDER={settings.recs_provider}",
                file=sys.stderr,
            )
            return EXIT_FAILED

        async with catalog_for(settings) as catalog:
            try:
                run = await run_recommendations(
                    session,
                    catalog,
                    model,
                    user_id=user.id,
                    prompt=args.prompt,
                    now=datetime.now(UTC),
                )
            except RecsRateLimited as exc:
                print(f"! {email}: {exc}", file=sys.stderr)
                return EXIT_FAILED
            except RecsEmptyPool:
                print(
                    f"! {email}: nothing to recommend from — seed a list first "
                    "(arc.cli demo-list) and let the catalogue sweep run",
                    file=sys.stderr,
                )
                return EXIT_FAILED
            except (RecsRefused, RecsUnavailable, RecsFailed) as exc:
                print(f"! {email}: the model did not answer ({exc})", file=sys.stderr)
                return EXIT_FAILED

    await session.commit()
    picks = run.picks or []
    print(f"run {run.id} for {email} — model {run.model or '(unknown)'}")
    print(f"  {len(picks)} entries from a pool of {len(run.candidates or [])} candidates")
    for pick in picks:
        title = str(pick.get("title") or f"anime {pick.get('anime_id')}")
        # A continuation carries ``because`` and a model pick carries ``case``
        # (FR-R6): two different claims, and the operator should see which.
        kind = "pick" if pick.get("case") else "franchise"
        print(f"  {kind:<10} {title}")
    remaining = await remaining_today(session, user_id=user.id, now=datetime.now(UTC))
    print(f"  {remaining} of {DAILY_LIMIT} runs left for this user today")
    return 0


# --- status -----------------------------------------------------------------


def _human_bytes(count: int) -> str:
    """``1536`` → ``1.5 KB``. Decimal units: it is a disk, not memory."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1000
    raise AssertionError("unreachable")  # pragma: no cover


async def _counts(session: AsyncSession, column: Any, model: Any) -> dict[str, int]:
    """``{value: n}`` for a grouped count, as plain strings."""
    rows = await session.execute(select(column, func.count()).select_from(model).group_by(column))
    return {str(getattr(value, "value", value)): int(total) for value, total in rows.all()}


def _table(counts: dict[str, int], order: Sequence[str]) -> str:
    """A grouped count on one line, in a fixed order, zeros included.

    Fixed order and explicit zeros because this is read by a person comparing
    two deployments, or the same deployment before and after a change: a state
    that vanishes when it hits zero is a line that moves.
    """
    known = ", ".join(f"{name} {counts.get(name, 0)}" for name in order)
    extra = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if k not in order)
    return f"{known}, {extra}" if extra else known


async def cmd_status(session: AsyncSession, settings: Settings, args: argparse.Namespace) -> int:
    """One screen: who is here, what is on disk, what the queue is doing.

    Read-only and safe to run against a live deployment. The catalogue section
    builds its own service and therefore its own circuit breaker, so it
    reports whether the sources *can* be reached now rather than what the
    long-running API process last saw — which is the question being asked
    when somebody runs this.
    """
    users = await _counts(session, User.role, User)
    active = await session.scalar(select(func.count()).select_from(User).where(User.is_active))
    linked = await session.scalar(select(func.count()).select_from(MalLink))
    entries = await _counts(session, ListEntry.status, ListEntry)
    episodes = await _counts(session, Episode.state, Episode)
    jobs = await _counts(session, Job.status, Job)
    shows = await session.scalar(select(func.count()).select_from(Anime))
    paused = await is_paused(session)
    retained = await retained_bytes(session, settings)

    total_users = sum(users.values())
    print(f"env             {settings.env}  (public_url {settings.public_url})")
    print(
        f"users           {total_users} total, {active or 0} active, {users.get('admin', 0)} admin"
    )
    print(f"mal accounts    {linked or 0} linked")
    print(f"catalogue rows  {shows or 0} anime")
    entry_states = [state.value for state in ListStatus]
    episode_states = [state.value for state in EpisodeState]
    job_states = [state.value for state in JobStatus]
    print(f"list entries    {sum(entries.values())} — {_table(entries, entry_states)}")
    print(f"episodes        {sum(episodes.values())} — {_table(episodes, episode_states)}")
    print(f"jobs            {sum(jobs.values())} — {_table(jobs, job_states)}")
    print(f"retained        {_human_bytes(retained)} on disk (sources + renditions)")
    print(f"acquisition     {'PAUSED' if paused else 'running'}")

    async with catalog_for(settings) as catalog:
        health = catalog.status()
    print(f"catalogue       active source: {health['active']}")
    for name, source in sorted(health["sources"].items()):
        state = source["state"]
        note = "" if source.get("configured", True) else " (unconfigured)"
        reason = f" — {source['reason']}" if source.get("reason") else ""
        print(f"  {name:<10}  {state}{note}{reason}")
    return 0


# --- wiring -----------------------------------------------------------------

type Command = Callable[[AsyncSession, Settings, argparse.Namespace], Awaitable[int]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc.cli",
        description="Arc operator commands (roadmap M11).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    invite = sub.add_parser("invite", help="issue an invite link and print it")
    invite.add_argument("--email", required=True, help="the address the invite is bound to")
    invite.add_argument(
        "--admin",
        action="store_true",
        help="promote this address to admin if it already has an account",
    )
    invite.add_argument(
        "--expires-in-hours",
        type=int,
        default=DEFAULT_EXPIRY_HOURS,
        help=f"invite lifetime in hours (default {DEFAULT_EXPIRY_HOURS}, i.e. 7 days)",
    )
    invite.set_defaults(handler=cmd_invite)

    warm = sub.add_parser(
        "warm-catalogue",
        help="queue the season pre-cache and a refresh of every followed show",
    )
    warm.set_defaults(handler=cmd_warm_catalogue)

    catalogue = sub.add_parser(
        "import-catalogue",
        help="download and import the offline catalogue now (manami + Fribb)",
    )
    catalogue.set_defaults(handler=cmd_import_catalogue)

    demo = sub.add_parser("demo-list", help="add shows to a user's list in one status")
    demo.add_argument("--user-email", required=True, help="whose list to add to")
    demo.add_argument(
        "--add",
        action="append",
        required=True,
        metavar="TITLE",
        help="a show title to search for and add; repeat for several",
    )
    demo.add_argument(
        "--status",
        choices=[state.value for state in ListStatus],
        default=ListStatus.WATCHING.value,
        help=f"the status every title here gets (default {ListStatus.WATCHING.value})",
    )
    demo.add_argument(
        "--progress",
        type=int,
        default=None,
        metavar="N",
        help="episodes watched, applied to every title in this invocation",
    )
    demo.add_argument(
        "--demo",
        action="store_true",
        help="flag this account as the demo one (users.is_demo); idempotent",
    )
    demo.set_defaults(handler=cmd_demo_list)

    recs = sub.add_parser("recs", help="produce one recommendation run for a user")
    recs.add_argument("--user-email", required=True, help="whose picks to produce")
    recs.add_argument(
        "--prompt",
        default=None,
        help="an optional mood prompt, exactly as the page's box (FR-R1)",
    )
    recs.set_defaults(handler=cmd_recs)

    status = sub.add_parser("status", help="print a summary of the deployment")
    status.set_defaults(handler=cmd_status)

    return parser


async def _run(handler: Command, settings: Settings, args: argparse.Namespace) -> int:
    """Own an engine for the length of one command and dispose of it."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            return await handler(session, settings, args)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings)
    return asyncio.run(_run(args.handler, settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
