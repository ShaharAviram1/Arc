"""users is_demo

The demo account flag (M16, owner 2026-09-18). One boolean on ``users``, and
everything it gates is presentation: the "How Arc works" entry in the nav and
the one-line strip on Watch Now that points at it. No acquisition rule, no MAL
rule and no media route reads it.

A column rather than a configured email address, because "which account is the
professor's" is a fact about this deployment and the database is the only place
a deployment keeps facts — and because an admin has to be able to take the flag
off again from the Users tab. ``NOT NULL DEFAULT false``, no backfill: every
account that exists is not the demo account, which is exactly what the default
says.

Revision ID: b63c05a9f1d2
Revises: 4f2ab7c91d68
Create Date: 2026-09-18 11:05:41.207318

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b63c05a9f1d2"
down_revision: Union[str, Sequence[str], None] = "4f2ab7c91d68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "is_demo")
