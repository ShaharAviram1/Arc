"""torrents manual flag

One column behind FR-A13 (owner 2026-10-06): a release a person chose by hand,
from Arc's candidates or by pasting a link, rather than one the ranker picked.
Nothing automatic replaces a manual choice on its own; the stall rule still
applies, and an episode whose chosen release stalled is told so.

``false`` for every existing row, which is the whole backfill: every torrent
Arc holds today was picked by the ranker. ``NOT NULL`` with a constant server
default is a catalogue-only change on PostgreSQL 11+ — no table rewrite.

Revision ID: f1a6d3c08b52
Revises: e4b7c2a91f05
Create Date: 2026-10-06 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a6d3c08b52"
down_revision: Union[str, Sequence[str], None] = "e4b7c2a91f05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "torrents",
        sa.Column("manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("torrents", "manual")
