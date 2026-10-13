"""wants.demanded_at and device_holdings

M20 T4 (owner 2026-10-13): play on demand and the device-copy skip.

- ``wants.demanded_at`` (nullable): the user pressed play on an episode that
  was not ready (FR-A14). Such a want is wanted even when a device holds the
  episode and even when the show waits for a slot. Null for every existing
  row, so the backfill is nothing and the add is catalogue-only.
- ``device_holdings``: the unwatched downloads each device of a user reported
  (FR-A15). Keyed (user, device, episode); indexed (user, reported_at) for
  the reconciler's lookup and the report's TTL purge. Both foreign keys cascade: a deleted user or episode
  takes its holdings with it.

Self-contained (no data step), so it can be re-parented onto another M20
revision without change.

Revision ID: 603a536051f9
Revises: 911eca1dfc62
Create Date: 2026-10-13 19:46:20.010918

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "603a536051f9"
down_revision: Union[str, Sequence[str], None] = "911eca1dfc62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "device_holdings",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("episode_id", sa.BigInteger(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_device_holdings_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_device_holdings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "device_id", "episode_id", name=op.f("pk_device_holdings")),
    )
    op.create_index(
        "ix_device_holdings_user_id_reported_at",
        "device_holdings",
        ["user_id", "reported_at"],
        unique=False,
    )
    op.add_column("wants", sa.Column("demanded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("wants", "demanded_at")
    op.drop_index("ix_device_holdings_user_id_reported_at", table_name="device_holdings")
    op.drop_table("device_holdings")
