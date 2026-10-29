"""offline_copies: redone_at

One column behind M20.4's redo (FR-P6, FR-A12, owner 2026-10-29): when a trip
episode's small copy was last re-made from a freshly fetched source, by the
operator's ``redo-copies`` or by the one automatic retry the encode makes when
its source turns out broken. Set, it means that automatic retry is spent: a
second failure on a broken source stays ``failed``.

Nullable with no default: no existing copy has been redone, so null is the true
backfill. A nullable ``ADD COLUMN`` is catalogue-only on PostgreSQL — no table
rewrite.

Revision ID: 8b6f2d4e1a73
Revises: 5d3e9a7c1b20
Create Date: 2026-10-29 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b6f2d4e1a73"
down_revision: Union[str, Sequence[str], None] = "5d3e9a7c1b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "offline_copies", sa.Column("redone_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the timestamp only. A copy redone before the downgrade could then be
    retried automatically once more after a broken source — the code before
    this revision never retries at all, so nothing reads the difference.
    """
    op.drop_column("offline_copies", "redone_at")
