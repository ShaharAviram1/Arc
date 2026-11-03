"""pack_picks: a season pack waiting for a person to pick an episode's file

M21 (owner 2026-11-03, R6, FR-A4, FR-D4; architecture.md §5.2 step 4): when
Arc cannot tell which of a pack's files is the wanted episode, the pack is
kept stopped in the client (``qbit_state = 'awaiting_pick'``) and one row here
records why and the candidate files, until a person picks one or dismisses it.

``uq_pack_picks_pending`` is a partial unique index on ``(torrent_id,
episode_id)`` where ``resolved_at IS NULL``: one pending pick per pack and
episode, resolved rows kept. ``ix_pack_picks_episode_id`` serves the
per-episode lookup and the episode's cascade delete.

The downgrade drops the table. No ``torrents`` row is touched either way: a
torrent left in ``awaiting_pick`` by a downgrade is a state the older code has
no name for, so its poll treats the pack as live again and overwrites the
state with the client's own on the next pass.

Revision ID: f6b2d0e4a859
Revises: e5a1c9d3f748
Create Date: 2026-11-03 12:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f6b2d0e4a859"
down_revision: Union[str, Sequence[str], None] = "e5a1c9d3f748"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "pack_picks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("torrent_id", sa.BigInteger(), nullable=False),
        sa.Column("episode_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=16), nullable=True),
        sa.Column("picked_file_index", sa.Integer(), nullable=True),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["torrent_id"], ["torrents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pack_picks_episode_id", "pack_picks", ["episode_id"], unique=False)
    op.create_index(
        "uq_pack_picks_pending",
        "pack_picks",
        ["torrent_id", "episode_id"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_pack_picks_pending", table_name="pack_picks")
    op.drop_index("ix_pack_picks_episode_id", table_name="pack_picks")
    op.drop_table("pack_picks")
