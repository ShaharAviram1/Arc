"""trips: device_id

A trip belongs to the device that started it (FR-A12, owner 2026-10-30): only
that device downloads its copies and confirms or releases them; the user's
other devices see the trip but leave it alone. The column holds the device's
canonical v4 UUID (the same text ``device_holdings.device_id`` stores).

Nullable with no default: trips made before this revision have no device, and
null is the true backfill. Such a trip is claimed by the first device that
confirms one of its copies or asks for one again (the one-way migration path,
done in code by ``arc.services.trips.deliver``). A nullable ``ADD COLUMN`` is
catalogue-only on PostgreSQL — no table rewrite.

Revision ID: 3f7a1c9e5b42
Revises: 8b6f2d4e1a73
Create Date: 2026-10-30 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f7a1c9e5b42"
down_revision: Union[str, Sequence[str], None] = "8b6f2d4e1a73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("trips", sa.Column("device_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the column: every trip becomes everybody's again, which is how the
    code before this revision treats all of them.
    """
    op.drop_column("trips", "device_id")
