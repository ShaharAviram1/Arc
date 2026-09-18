"""seed the batch_fallback setting

The kill switch for FR-A4's batch exception (FR-A11, FR-D2, owner 2026-09-18).
A **finished** show with no acceptable single at all may take a batch that
covers the wanted episode and download only that episode's file; this is the
one key that turns that off, and it is read in exactly one place —
``search_release``'s batch branch — so turning it off leaves every other
acquisition path byte-identical rather than merely quieter.

``true`` by default: it is the behaviour the owner asked for, and for a show
that finished airing years ago it is often the difference between an episode
and a fortnight of "searching". It is here as a switch rather than as an env
var because it is an admin-editable rule (FR-D2), and as a migration of its own
rather than an edit to the initial seed for the reason ``9c4a17e5db20``,
``5e0a92c7b431`` and ``7c1d5b3ae4f2`` all give: a database that has already run
the initial schema would never see a key added to that INSERT. ``ON CONFLICT DO
NOTHING`` so an admin who has already turned it off by hand keeps it off.

Data only; no schema change, so autogenerate still finds nothing.

Revision ID: d81f4b6ca3e7
Revises: c3f81a5d27be
Create Date: 2026-09-18 17:45:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d81f4b6ca3e7"
down_revision: Union[str, Sequence[str], None] = "c3f81a5d27be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "batch_fallback"
DEFAULT = "true"


def upgrade() -> None:
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{KEY}', '{DEFAULT}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key = '{KEY}'")
