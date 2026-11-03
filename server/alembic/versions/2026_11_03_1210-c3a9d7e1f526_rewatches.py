"""rewatches: a user's rewatch passes of a show

M21 (owner 2026-11-03, R2, FR-W7): one row per pass, open while ``ended_at``
is null, ended with ``end_reason`` ``off`` or ``finished``. Ended rows are
kept so an offline report is judged against the pass open when the device
recorded it (FR-S8).

``uq_rewatches_open`` is a partial unique index on ``(user_id, anime_id)``
where ``ended_at IS NULL``: one open pass per user and show.
``ix_rewatches_user_id_anime_id_started_at`` serves "the pass open at time t
for (user, show)" and, by its prefix, "this user's passes".

The downgrade drops the table and its passes.

Revision ID: c3a9d7e1f526
Revises: b1e5f2a8c4d0
Create Date: 2026-11-03 12:10:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a9d7e1f526"
down_revision: Union[str, Sequence[str], None] = "b1e5f2a8c4d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rewatches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("anime_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["anime_id"], ["anime.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rewatches_user_id_anime_id_started_at",
        "rewatches",
        ["user_id", "anime_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "uq_rewatches_open",
        "rewatches",
        ["user_id", "anime_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_rewatches_open", table_name="rewatches")
    op.drop_index("ix_rewatches_user_id_anime_id_started_at", table_name="rewatches")
    op.drop_table("rewatches")
