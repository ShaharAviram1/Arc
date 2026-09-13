"""episodes last search diagnostic

What the last ``search_release`` for this episode asked and saw (FR-A7,
2026-09-14): when it ran, how many query forms it ran, and how many distinct
releases they returned between them before the filter. The show page renders
the three as one line — "Searching · 6 forms, 0 results · next try 23:26" —
because a row that only says ``Searching`` for six hours cannot tell an owner
whether the queries find nothing or the filter keeps nothing, and both of
those happened on production this week.

Nullable with no backfill and no default: an episode nobody has searched for
has nothing to say here, and the numbers arrive on the next attempt. Smallints
because the cap on the forms is ten (``nyaa.MAX_QUERIES``) and Nyaa's RSS
returns at most 75 items a page.

Revision ID: 4f2ab7c91d68
Revises: 7c1d5b3ae4f2
Create Date: 2026-09-14 09:10:12.884215

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f2ab7c91d68"
down_revision: Union[str, Sequence[str], None] = "7c1d5b3ae4f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "episodes", sa.Column("last_search_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("episodes", sa.Column("last_search_forms", sa.SmallInteger(), nullable=True))
    op.add_column("episodes", sa.Column("last_search_results", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("episodes", "last_search_results")
    op.drop_column("episodes", "last_search_forms")
    op.drop_column("episodes", "last_search_at")
