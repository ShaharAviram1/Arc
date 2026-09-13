"""seed the min_free_gb setting

The storage floor (FR-T6, owner 2026-09-13). While free space on the data
volume is below it acquisition **holds itself**: the reconciler still runs —
everything it does when space is short frees space — but no new search starts,
and it resumes on its own once retention has made room. Nobody presses it and
nobody has to clear it, which is the difference between a hold and the pause
beside it.

10 GB by default: about two episodes' source plus their renditions plus the
transcode's scratch, which is the smallest margin that still leaves the machine
somewhere to put what it is already holding. Editable in the rules editor like
G, D and N (FR-T5's argument, applied to a fourth number), 0 turns the guard
off.

A row rather than an env var, and a migration of its own rather than an edit to
the initial seed, for the two reasons ``9c4a17e5db20`` gives: it is an
admin-editable rule, and a database that has already run the initial schema
would never see a key added to that INSERT. ``ON CONFLICT DO NOTHING`` so an
admin who has already set a floor by hand keeps it.

Data only; no schema change, so autogenerate still finds nothing.

Revision ID: 5e0a92c7b431
Revises: 3b7c41e9d2af
Create Date: 2026-09-13 12:05:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e0a92c7b431"
down_revision: Union[str, Sequence[str], None] = "3b7c41e9d2af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "min_free_gb"
DEFAULT = 10


def upgrade() -> None:
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{KEY}', '{DEFAULT}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key = '{KEY}'")
