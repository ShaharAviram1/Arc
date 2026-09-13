"""seed the slot_cap_k setting

K — how many shows one user may have fetching at once (FR-A10, owner
2026-09-13). The fourth brake on acquisition and the only per-user one: shows
already fetching keep their slot, free slots go to currently airing shows first
and then to the most recently touched entries, and the rest wait visibly on
their own show page until one of the others finishes.

5 by default: what one disk and qBittorrent's handful of download slots
actually allow to arrive at the same time. 0 means *unlimited*, which is the
opposite of what 0 means for ``look_ahead_n`` beside it — a cap of nothing is
no cap, while a window of nothing is no fetching.

A row rather than an env var, and a migration of its own rather than an edit to
the initial seed, for the two reasons ``9c4a17e5db20`` and ``5e0a92c7b431``
give: it is an admin-editable rule, and a database that has already run the
initial schema would never see a key added to that INSERT. ``ON CONFLICT DO
NOTHING`` so an admin who has already set a cap by hand keeps it.

Data only; no schema change, so autogenerate still finds nothing.

Revision ID: 7c1d5b3ae4f2
Revises: 5e0a92c7b431
Create Date: 2026-09-13 18:10:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1d5b3ae4f2"
down_revision: Union[str, Sequence[str], None] = "5e0a92c7b431"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "slot_cap_k"
DEFAULT = 5


def upgrade() -> None:
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{KEY}', '{DEFAULT}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key = '{KEY}'")
