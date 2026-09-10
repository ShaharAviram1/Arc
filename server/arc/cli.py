"""Operator commands: ``python -m arc.cli <command>`` (roadmap M11).

Three things an operator has to do on a host with no browser session yet, and
one thing they always want to know:

* ``invite``          — issue an invite link and print it.
* ``warm-catalogue``  — queue the work that gives a fresh deployment a
                        schedule and covers, instead of waiting for 03:30 UTC.
* ``demo-list``       — put a show on somebody's list, by title.
* ``status``          — a one-screen summary of the deployment.

Every one is **idempotent**: ``demo-list`` twice leaves one entry, the two
enqueueing commands deduplicate on the job type, and ``status`` writes nothing.
Re-running the lot after a failed deploy is a supported thing to do.

These are thin wrappers over the same services the API calls — nothing here
reimplements a rule. ``demo-list`` in particular goes through
:func:`~arc.services.catalog.lists.set_list_entry`, so it recomputes the
acquisition window and respects the MAL write rules exactly as the endpoint
does (FR-C2, FR-W2, FR-M7).

Run it from ``server/``::

    uv run python -m arc.cli status
    uv run python -m arc.cli invite --email prof@example.edu
    uv run python -m arc.cli demo-list --user-email prof@example.edu \\
        --add "Sousou no Frieren" --add "Vinland Saga"
    uv run python -m arc.cli warm-catalogue

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
from arc.services.catalog import SourceUnavailable, preferred_title, set_list_entry
from arc.services.catalog.cache import upsert_summaries
from arc.services.catalog.factory import catalog_for
from arc.services.catalog.names import CATALOG_PRIORITY, REFRESH_ALL, SEASON_SWEEP
from arc.services.jobs import enqueue
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


# --- demo-list --------------------------------------------------------------


async def cmd_demo_list(session: AsyncSession, settings: Settings, args: argparse.Namespace) -> int:
    """Add shows to a user's list as *watching*, looked up by title.

    The point is a fresh deployment where the person who is about to log in
    has an empty Home page and an empty schedule. One command puts real shows
    on their list, which is what Home, the schedule highlights and acquisition
    all key off.

    The lookup is the catalogue's own search, so it needs AniList (or MAL) to
    be reachable; the top hit is taken and printed, and a title that matches
    nothing is reported rather than guessed at. Adding the same show twice is
    a no-op beyond a touched ``updated_at``, because ``set_list_entry``
    describes a state rather than an event.
    """
    email = normalize_email(args.user_email)
    user = await get_by_email(session, email)
    if user is None:
        print(f"no user with the address {email}", file=sys.stderr)
        return EXIT_FAILED

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
            await set_list_entry(
                session,
                catalog,
                user_id=user.id,
                anime_id=anime.id,
                status=ListStatus.WATCHING,
            )
            await session.commit()
            print(f"+ {preferred_title(anime)}  (anime {anime.id}) → watching")

    return EXIT_FAILED if failures else 0


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

    demo = sub.add_parser("demo-list", help="add shows to a user's list as watching")
    demo.add_argument("--user-email", required=True, help="whose list to add to")
    demo.add_argument(
        "--add",
        action="append",
        required=True,
        metavar="TITLE",
        help="a show title to search for and add; repeat for several",
    )
    demo.set_defaults(handler=cmd_demo_list)

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
