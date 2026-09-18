"""Acquisition: ``wants`` and ``torrents`` (architecture.md §4, §5.1).

A ``Want`` is one user's interest in one episode. Wants from all users merge:
an episode with any live want is fetched once (FR-A2). A ``Torrent`` is the
Nyaa release picked for an episode and handed to qBittorrent, and a
``TorrentFile`` is one file inside a batch and whether Arc asked for it
(FR-A11).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, created_at
from arc.models.enums import TorrentKind, enum_column

#: ``ck_torrents_kind_episode``: a single names its episode and a batch names
#: none. Written out here and spelled once, because the migration that creates
#: it needs the same text and a CHECK autogenerate does not compare is a CHECK
#: nothing else would notice drifting.
KIND_EPISODE_PREDICATE = (
    "(kind = 'single' AND episode_id IS NOT NULL) OR (kind = 'batch' AND episode_id IS NULL)"
)

#: The partial index on ``torrent_files.episode_id`` — the lookup every batch
#: path makes ("which file holds this episode?"), restricted to the rows that
#: name one, because a pack's fonts and NCOPs never do.
EPISODE_FILE_INDEX = "ix_torrent_files_episode_id"
EPISODE_FILE_PREDICATE = "episode_id IS NOT NULL"

#: And the invariant behind "one batch, several wants": an episode has at most
#: **one** live claim anywhere. A partial *unique* index rather than a rule in
#: code because three modules write ``wanted`` — the pick, the reconciler and
#: retention — and the database is the only place all three agree.
WANTED_CLAIM_INDEX = "ux_torrent_files_one_wanted_per_episode"
WANTED_CLAIM_PREDICATE = "wanted AND episode_id IS NOT NULL"


class Want(Base):
    """(user, episode) → "this user wants this episode" (FR-A1).

    The table is a **reconciled** view of everybody's list, rebuilt by
    :func:`arc.services.acquisition.wants.compute_wants`, and the two ways a
    want ends are deliberately different.

    A want that lapses because the *window* moved — the user watched ahead, or
    N was turned down — is **deleted**. There is nothing to remember: the
    window is recomputed from the list every fifteen minutes, so the row would
    come back the moment it applied again, and where it matters (the user
    watched the episode) ``watch_progress.completed_at`` is a better record of
    the same fact than a tombstone would be.

    A want that stops because the user stopped wanting it is **kept**, with
    ``dropped_at`` and ``drop_reason``. Two rules end that way: FR-W4's "the
    show is no longer watching/planned" and FR-T2's "you have had this ready
    for D days and have not watched it" (M10). Both are statements about a user
    and an episode rather than about the window, both must survive the next
    recompute, and both are what retention counts FR-T1's grace period from —
    an episode whose last want was dropped this morning is not a month-old file
    nobody ever asked for, however old its bytes are.

    ``sample`` marks the one row the reconciler did not derive: "try episode 1"
    (FR-A8), a want a user asked for by hand on a show that is not on their
    list. Everything else about it is an ordinary want — the same states, the
    same D-day drop, the same retention — which is why it is a flag on this
    table rather than a table of its own.
    """

    __tablename__ = "wants"
    __table_args__ = (
        # The acquisition question is always "does this episode still have a
        # live want?", so the index covers only rows that are still live.
        Index(
            "ix_wants_episode_id_active",
            "episode_id",
            postgresql_where=text("dropped_at IS NULL"),
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = created_at()
    #: Null while the want is live. Set when the show stopped being
    #: watching/planned — on hold, dropped, completed, off the list (FR-W4) —
    #: or the episode went unwatched for D days (FR-T2).
    dropped_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    drop_reason: Mapped[str | None] = mapped_column(String(64))
    #: A want the user created explicitly for the show's first episode (FR-A8)
    #: rather than one the reconciler derived from their list. It is the single
    #: exception to "only the next N episodes of a watching/planned show": the
    #: row survives a reconciliation that would otherwise have nothing to
    #: justify it, and once it is dropped — cancelled, or gone unwatched for D
    #: days (FR-T2) — the reconciler leaves it dropped rather than reviving it.
    sample: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class Torrent(Base):
    """The Nyaa release chosen for an episode, or a batch, and its qBit state.

    Two shapes, told apart by :attr:`kind` (FR-A4, FR-A11, owner 2026-09-18).

    A ``single`` is what every row written before then is: one release whose
    whole payload is one episode's, with :attr:`episode_id` set. A ``batch`` is
    the exception a **finished** show with no acceptable single earns — one
    release covering several episodes, with :attr:`episode_id` **null** and one
    :class:`TorrentFile` row per file saying which episode it holds and whether
    Arc asked for it. Only the asked-for files are ever downloaded.

    The null is the whole of the design. Every query keyed on ``episode_id`` —
    the reconciler's cancel, ``qbit_cancel``, ``reject_download``, retention's
    ``torrent_hashes``, ``poll_qbit``'s join — therefore *skips* a batch by
    default, which matters because each of them would otherwise delete a
    torrent several episodes share, with its files. ``ck_torrents_kind_episode``
    makes the pairing a database fact rather than a convention.
    """

    __tablename__ = "torrents"
    __table_args__ = (
        Index("ix_torrents_episode_id", "episode_id"),
        CheckConstraint(KIND_EPISODE_PREDICATE, name="kind_episode"),
    )

    id: Mapped[int] = bigint_pk()
    #: Null exactly when :attr:`kind` is ``batch``: a batch belongs to no
    #: single episode, and its per-episode claims live in ``torrent_files``.
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"),
    )
    #: 40 hex chars for a v1 hash, 64 for v2; sized for the longer one.
    #: Unique: the hash *is* the torrent's identity, and qBittorrent keys its
    #: own state by it, so two rows for one hash would be two views of one
    #: download. It is also what resolves two searches racing on the same
    #: batch: the loser's flush raises, its job retries, and the retry attaches
    #: to the row the winner wrote rather than adding the pack twice.
    info_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    magnet: Mapped[str | None] = mapped_column(Text)
    #: The raw release title, kept for debugging the ranker.
    title: Mapped[str | None] = mapped_column(Text)
    #: Release group, as parsed. Quoted in SQL: ``group`` is a keyword.
    group: Mapped[str | None] = mapped_column("group", String(64))
    resolution: Mapped[str | None] = mapped_column(String(16))
    #: Seeders *at pick time* — a ranking input, not a live figure (FR-A3).
    seeders: Mapped[int | None] = mapped_column(Integer)
    trusted: Mapped[bool | None] = mapped_column(Boolean)
    #: qBittorrent's own state string (downloading, stalledDL, …), or one of
    #: Arc's **decisions** about this download, which are not states the client
    #: has an opinion about and are never overwritten by a poll: ``rejected``
    #: (a person said the delivered file was not this episode), ``stalled``
    #: (it was going nowhere and was removed with its files), ``cancelled``
    #: (nobody wanted it any more) and ``missing`` (gone from the client for
    #: some reason that was not Arc's).
    qbit_state: Mapped[str | None] = mapped_column(String(32))
    #: 0..1 download progress, polled every 60 s (§5.1 step 4). For a batch it
    #: is the client's figure for the **selected** files, and an episode's own
    #: progress is its ``torrent_files`` row's.
    progress: Mapped[float | None] = mapped_column(Float)
    kind: Mapped[TorrentKind] = mapped_column(
        enum_column(TorrentKind),
        nullable=False,
        default=TorrentKind.SINGLE,
        server_default=TorrentKind.SINGLE.value,
    )
    #: The save path as the client was told it, container-side. A single's is
    #: ``<downloads>/<episode id>`` and derivable from the row, which is why
    #: nothing has needed this column until now; a batch's is
    #: ``<downloads>/batch/<info hash>`` and is not, and ``host_path`` needs it
    #: to reach a file inside the pack.
    save_path: Mapped[str | None] = mapped_column(Text)
    #: The whole payload, bytes. Logged and reported, never reasoned with.
    total_size: Mapped[int | None] = mapped_column(BigInteger)
    #: The sum of the **selected** files, bytes. This is the figure any rule,
    #: log line or reservation may use for a batch (FR-A11): the point of the
    #: exception is that the 14 GB pack costs 1.1 GB of disk.
    wanted_bytes: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = created_at()
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class TorrentFile(Base):
    """One file inside a torrent, and whether Arc asked for it (FR-A11).

    Written **only** for a ``kind = 'batch'`` torrent. "No ``torrent_files``
    rows at all" therefore keeps meaning "the whole payload is one episode's",
    which is what every path written before FR-A11 assumes — and is why the
    batch work needed no backfill.

    :attr:`episode_id` is the episode the filename parser read out of the
    file's own name at pick time. It is **not** an authoritative link: it is
    fed to the matcher as the existing ``expected`` prior, and FR-L4's
    confidence and title floors still decide, because "never auto-link a guess"
    is a non-negotiable and a second linker is exactly the code that would one
    day link the wrong episode.
    """

    __tablename__ = "torrent_files"
    __table_args__ = (
        Index(
            EPISODE_FILE_INDEX,
            "episode_id",
            postgresql_where=text(EPISODE_FILE_PREDICATE),
        ),
        Index(
            WANTED_CLAIM_INDEX,
            "episode_id",
            unique=True,
            postgresql_where=text(WANTED_CLAIM_PREDICATE),
        ),
        Index("ux_torrent_files_torrent_index", "torrent_id", "file_index", unique=True),
    )

    id: Mapped[int] = bigint_pk()
    torrent_id: Mapped[int] = mapped_column(
        ForeignKey("torrents.id", ondelete="CASCADE"), nullable=False
    )
    #: The index ``torrents/files`` reports and ``torrents/filePrio`` takes.
    #: Unique per torrent, which is the client's own guarantee written down.
    file_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: As the torrent names it, relative to the torrent's ``save_path``.
    path: Mapped[str] = mapped_column(Text, nullable=False)
    #: Bytes. BIGINT: one file of a 1080p pack passes the 2 GB INT ceiling.
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The episode this file holds, per the parser. ``SET NULL`` rather than
    #: cascade: the file is still in the pack after the episode row goes.
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"),
    )
    #: Whether Arc set this file's priority to 1 — the live claim. An episode
    #: has at most one anywhere (:data:`WANTED_CLAIM_INDEX`), and un-wanting a
    #: row is how cancel, reject and retention give a file back without
    #: touching the torrent the other episodes are still using.
    wanted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    #: 0..1, **this file's** own, from ``torrents/files``. An episode is
    #: complete when its file is, not when the torrent is.
    progress: Mapped[float | None] = mapped_column(Float)
    #: What Arc last wrote to the client (0 or 1), for the audit. Kept beside
    #: :attr:`wanted` rather than derived from it, so a selection that failed
    #: to be written is distinguishable from one that was never asked for.
    priority: Mapped[int | None] = mapped_column(SmallInteger)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
