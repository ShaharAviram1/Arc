"""wants sample flag

One column behind "try episode 1" (FR-A8, M16): the want a user asked for by
hand, on a show that is not on their list, so they can decide whether they
like it before adding it.

``false`` for every row that exists, and for every row the reconciler writes
afterwards — which is the whole of the backfill, because every want Arc has
today was derived from somebody's list. A ``NOT NULL`` with a server default is
safe on a table this size (one row per user per wanted episode) and keeps the
column's meaning single: a want either was asked for or was worked out.

Revision ID: 73f2f8eac7e7
Revises: a5f4ae93df3d
Create Date: 2026-09-12 20:31:50.606586

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "73f2f8eac7e7"
down_revision: Union[str, Sequence[str], None] = "a5f4ae93df3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "wants",
        sa.Column("sample", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("wants", "sample")
