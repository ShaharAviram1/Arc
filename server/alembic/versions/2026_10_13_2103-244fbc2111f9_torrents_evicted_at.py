"""torrents_evicted_at

One column behind M20 T9's remembered release (FR-T3, FR-A6): when retention
removed a single's bytes. Retention used to delete the ``torrents`` row with the
files; it now keeps a single's row as ``qbit_state = 'evicted'`` with this
timestamp, so a later want re-adds the same release before asking Nyaa, and a
re-fetch (a live row with ``evicted_at`` set) is the one that falls back to a
search within ``REFETCH_DEAD_MINUTES`` when its swarm is dead.

Nullable with no default: every existing row is a release whose bytes retention
has not removed (it deleted those rows outright), so null is the true backfill.
A nullable ``ADD COLUMN`` is catalogue-only on PostgreSQL — no table rewrite.

Revision ID: 244fbc2111f9
Revises: caf1811e8ee6
Create Date: 2026-10-13 21:03:31.235078

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "244fbc2111f9"
down_revision: Union[str, Sequence[str], None] = "caf1811e8ee6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("torrents", sa.Column("evicted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the timestamp only. Rows already marked ``evicted`` keep that state
    string, which the code before this revision does not know: it reads them as
    a live state with no torrent in the client and marks them ``missing`` on the
    next poll, which is harmless for an episode retention reset to
    ``not_wanted``.
    """
    op.drop_column("torrents", "evicted_at")
