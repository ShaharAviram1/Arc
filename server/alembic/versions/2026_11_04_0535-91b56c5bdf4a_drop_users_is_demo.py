"""drop users is_demo

M21 (owner 2026-11-03, R5): the demo ended and the demo account is gone, so
the flag that marked it goes too (it was added by ``b63c05a9f1d2``, M16). Only
the column is dropped; no row is deleted — the demo user was already removed
from production on 2026-10-15. ``UserOut.is_demo`` stays on the API as a
constant ``false`` for one release so a cached client keeps working.

The downgrade re-adds the column ``NOT NULL DEFAULT false``, which is what
every account was in the end.

Revision ID: 91b56c5bdf4a
Revises: a8c3e1f5b960
Create Date: 2026-11-04 05:35:02.414656

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "91b56c5bdf4a"
down_revision: Union[str, Sequence[str], None] = "a8c3e1f5b960"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("users", "is_demo")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "users",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
