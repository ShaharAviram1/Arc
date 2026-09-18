"""torrent_files and batch kind

The data model for FR-A4's batch exception and FR-A11 (M16, owner 2026-09-18).
A **finished** show with no acceptable single may take a batch that covers the
wanted episode and download only that episode's file, so a ``torrents`` row is
no longer always one episode's: ``episode_id`` becomes **nullable** and is null
exactly when the new ``kind`` column says ``batch``, tied together by
``ck_torrents_kind_episode``. That null is the point — every query keyed on
``episode_id`` (the reconciler's cancel, ``qbit_cancel``, ``reject_download``,
retention's hashes, ``poll_qbit``'s join) then skips a batch by default, and
each of them would otherwise delete a torrent several episodes share with its
files.

``save_path`` is added because a batch's path (``<downloads>/batch/<hash>``) is
not derivable from the row the way a single's (``<downloads>/<episode id>``)
is, and ``total_size``/``wanted_bytes`` because the whole claim of the feature
is that the 14 GB pack costs the 1.1 GB of its selected files — the second
figure is the only one any rule may read.

``torrent_files`` carries the per-file claims. Three indexes, all written out
explicitly: ``UNIQUE (torrent_id, file_index)``, a partial index on
``episode_id`` where it is set (a pack's fonts and NCOPs never name one), and a
partial **unique** index on ``episode_id WHERE wanted`` — the invariant behind
"one batch, several wants", which is a database fact rather than a rule in code
because the pick, the reconciler and retention all write ``wanted``.

**No backfill.** Every existing row is a single: ``kind`` defaults to
``single``, ``episode_id`` stays set, the three new nullable columns stay null
and nothing reads them for a single. ``torrent_files`` is populated only for
batches, so "no rows at all" keeps meaning "the whole payload is one episode's".

**A batch row must be deleted before downgrading.** ``downgrade()`` restores
``episode_id NOT NULL``, which a ``kind = 'batch'`` row cannot satisfy — and
deleting one means telling qBittorrent about it too, which a migration is the
wrong place to do.

Revision ID: c3f81a5d27be
Revises: b63c05a9f1d2
Create Date: 2026-09-18 15:20:11.884502

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f81a5d27be"
down_revision: Union[str, Sequence[str], None] = "b63c05a9f1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out here rather than imported from ``arc.models.acquisition``, which
# spells all five the same way: a migration is a historical record and must keep
# applying identically after those constants move on — the same reason
# ``ix_watch_progress_in_progress`` repeats its own predicate.
KIND_EPISODE_PREDICATE = (
    "(kind = 'single' AND episode_id IS NOT NULL) OR (kind = 'batch' AND episode_id IS NULL)"
)
EPISODE_FILE_INDEX = "ix_torrent_files_episode_id"
EPISODE_FILE_PREDICATE = "episode_id IS NOT NULL"
WANTED_CLAIM_INDEX = "ux_torrent_files_one_wanted_per_episode"
WANTED_CLAIM_PREDICATE = "wanted AND episode_id IS NOT NULL"
TORRENT_INDEX_INDEX = "ux_torrent_files_torrent_index"


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("torrents", "episode_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column(
        "torrents",
        sa.Column(
            "kind",
            sa.Enum("single", "batch", name="torrentkind", native_enum=False, length=32),
            nullable=False,
            server_default="single",
        ),
    )
    op.add_column("torrents", sa.Column("save_path", sa.Text(), nullable=True))
    op.add_column("torrents", sa.Column("total_size", sa.BigInteger(), nullable=True))
    op.add_column("torrents", sa.Column("wanted_bytes", sa.BigInteger(), nullable=True))
    # Written out rather than left to a model-level default: the pairing is the
    # thing that keeps a batch out of every episode-keyed query.
    op.create_check_constraint(op.f("ck_torrents_kind_episode"), "torrents", KIND_EPISODE_PREDICATE)

    op.create_table(
        "torrent_files",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("torrent_id", sa.BigInteger(), nullable=False),
        sa.Column("file_index", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("episode_id", sa.BigInteger(), nullable=True),
        sa.Column("wanted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("priority", sa.SmallInteger(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_torrent_files_episode_id_episodes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["torrent_id"],
            ["torrents.id"],
            name=op.f("fk_torrent_files_torrent_id_torrents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_torrent_files")),
    )
    # Partial, because most of a pack's files name no episode.
    op.create_index(
        EPISODE_FILE_INDEX,
        "torrent_files",
        ["episode_id"],
        unique=False,
        postgresql_where=sa.text(EPISODE_FILE_PREDICATE),
    )
    # And partial *unique*: one live claim per episode, anywhere.
    op.create_index(
        WANTED_CLAIM_INDEX,
        "torrent_files",
        ["episode_id"],
        unique=True,
        postgresql_where=sa.text(WANTED_CLAIM_PREDICATE),
    )
    op.create_index(TORRENT_INDEX_INDEX, "torrent_files", ["torrent_id", "file_index"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(TORRENT_INDEX_INDEX, table_name="torrent_files")
    op.drop_index(
        WANTED_CLAIM_INDEX,
        table_name="torrent_files",
        postgresql_where=sa.text(WANTED_CLAIM_PREDICATE),
    )
    op.drop_index(
        EPISODE_FILE_INDEX,
        table_name="torrent_files",
        postgresql_where=sa.text(EPISODE_FILE_PREDICATE),
    )
    op.drop_table("torrent_files")

    op.drop_constraint(op.f("ck_torrents_kind_episode"), "torrents", type_="check")
    op.drop_column("torrents", "wanted_bytes")
    op.drop_column("torrents", "total_size")
    op.drop_column("torrents", "save_path")
    op.drop_column("torrents", "kind")
    # Fails on a batch row, by design: see the module docstring.
    op.alter_column("torrents", "episode_id", existing_type=sa.BigInteger(), nullable=False)
