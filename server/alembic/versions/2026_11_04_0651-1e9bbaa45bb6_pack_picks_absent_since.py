"""pack picks absent since

M21 T10: a pack waiting for a pick counts as gone from the torrent client only
when two reconcile passes at least an hour apart both find it missing from the
client's listing (``pack_picks.gone_from``). The first such pass stamps this
column; a listing that shows the pack again clears it. Nullable with no
default: every existing pick starts as "not seen missing yet", so the first
pass after the deploy can stamp but never dismiss.

Revision ID: 1e9bbaa45bb6
Revises: 91b56c5bdf4a
Create Date: 2026-11-04 06:51:27.913240

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1e9bbaa45bb6"
down_revision: Union[str, Sequence[str], None] = "91b56c5bdf4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "pack_picks", sa.Column("absent_since", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pack_picks", "absent_since")
