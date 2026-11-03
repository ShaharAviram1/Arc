"""watch_progress.finished_at: the latest finish, backfilled from completed_at

M21 (owner 2026-11-03, R1): ``watch_progress.finished_at`` is the latest
moment a user crossed the completion mark or marked the episode by hand, in
any mode, cleared by an un-mark. ``completed_at`` stays the once-written first
watch. It ends a demand, counts a rewatch pass and anchors retention's watched
rule (architecture.md §4, §5.1a, §5.5, §5.7).

Backfilled ``finished_at = completed_at WHERE completed``: for every row that
exists today the first watch is the only finish Arc recorded. A nullable
column with no default is catalogue-only on PostgreSQL; the backfill is one
``UPDATE`` over the completed rows.

The downgrade drops the column; ``completed_at`` was never touched.

Revision ID: b1e5f2a8c4d0
Revises: a7c2e4f9d1b3
Create Date: 2026-11-03 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1e5f2a8c4d0"
down_revision: Union[str, Sequence[str], None] = "a7c2e4f9d1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "watch_progress", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE watch_progress SET finished_at = completed_at WHERE completed")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("watch_progress", "finished_at")
