"""show_pins

One table behind FR-T8 (owner 2026-10-13, M20 T8): a user's "Keep files" on
a show. Delete after watching (FR-T1) skips every episode of a show any user
has pinned; only the disk floor (FR-T6) may still remove them.

The pair is the primary key, so a second pin is the same row; both foreign
keys cascade, so a deleted account or show takes its pins with it. No backfill:
nothing was pinned before this revision. No index beyond the key: the table
holds a few rows per user, and the sweep reads it whole once an hour.

Revision ID: 911eca1dfc62
Revises: a7d2e5c9f318
Create Date: 2026-10-13 19:43:22.645468

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "911eca1dfc62"
down_revision: Union[str, Sequence[str], None] = "a7d2e5c9f318"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "show_pins",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("anime_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["anime_id"],
            ["anime.id"],
            name=op.f("fk_show_pins_anime_id_anime"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_show_pins_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "anime_id", name=op.f("pk_show_pins")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("show_pins")
