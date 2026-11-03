"""walks: where a user's look-ahead on a show starts after a play

M21 (owner 2026-11-03, R3, FR-A1): ``(user_id, anime_id)`` → ``after_number``
(k−1 while episode k plays, k once it is finished) and ``touched_at``. Written
only by ``record_progress``. The table starts empty on purpose: deriving walks
from the existing ``watch_progress`` rows would start them on long-dropped
shows at deploy.

The downgrade drops the table.

Revision ID: d4f8b2c6e937
Revises: c3a9d7e1f526
Create Date: 2026-11-03 12:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f8b2c6e937"
down_revision: Union[str, Sequence[str], None] = "c3a9d7e1f526"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "walks",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("anime_id", sa.BigInteger(), nullable=False),
        sa.Column("after_number", sa.Integer(), nullable=False),
        sa.Column("touched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["anime_id"], ["anime.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "anime_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("walks")
