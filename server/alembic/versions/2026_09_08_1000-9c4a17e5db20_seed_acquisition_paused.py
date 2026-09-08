"""seed the acquisition_paused setting

The acquisition kill switch (spec §4.2). ``compute_wants`` does nothing while
it is set and ``search_release`` requeues itself without touching Nyaa, so an
admin can stop Arc fetching without stopping the worker — which is what the
day a MAL import adds fifteen watching shows at once needs.

A row rather than a column: it is an admin-editable rule, and rules live in
``settings`` (the same argument ``arc.services.acquisition.rules`` makes for
the per-show overrides). A migration of its own rather than an edit to the
initial schema's seed, because a migration is a historical record: a database
that has already run ``4d1c3479c036`` would never see a key added to that
INSERT.

``ON CONFLICT DO NOTHING`` for the same reason the initial seed has it — an
admin who has already paused acquisition by hand must not be un-paused by an
upgrade.

Data only; no schema change, so autogenerate still finds nothing.

Revision ID: 9c4a17e5db20
Revises: 6e2f1a8b93c4
Create Date: 2026-09-08 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c4a17e5db20"
down_revision: Union[str, Sequence[str], None] = "6e2f1a8b93c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "acquisition_paused"


def upgrade() -> None:
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{KEY}', 'false'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key = '{KEY}'")
