"""seed the M20 settings and move look_ahead_n from 2 to 1

M20 (owner 2026-10-13): one episode ready per watcher instead of two, with one
more fetched at the halfway mark (FR-A1, M20 T4), and two new admin-editable
rules whose behaviour lands later in M20 (FR-D2):

- ``watched_grace_hours`` = 6: hours an episode's files survive the last
  completion under delete-after-watching (FR-T1, M20 T8).
- ``min_seeders_warn`` = 3: fewer seeders than this on a fetch raises a "few
  seeders" notice on the show (FR-A16, M20 T9).

Both inserted with ``ON CONFLICT DO NOTHING`` so a value already set by hand is
kept, for the reason ``d81f4b6ca3e7`` gives for a migration of its own.

``look_ahead_n`` moves to 1 **only where it is still exactly 2**, the initial
migration's seed (owner Q9, 2026-10-13): a value an admin chose is left alone.
The downgrade puts 2 back only where the value is 1, and removes the two keys.

Data only; no schema change, so autogenerate finds nothing.

Revision ID: a7d2e5c9f318
Revises: f1a6d3c08b52
Create Date: 2026-10-13 19:01:33.063484

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d2e5c9f318"
down_revision: Union[str, Sequence[str], None] = "f1a6d3c08b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: key → JSON text of its default, stored as ``jsonb`` like every other row.
NEW_KEYS: dict[str, str] = {
    "watched_grace_hours": "6",
    "min_seeders_warn": "3",
}

LOOK_AHEAD_KEY = "look_ahead_n"


def upgrade() -> None:
    for key, value in NEW_KEYS.items():
        op.execute(
            f"INSERT INTO settings (key, value) VALUES ('{key}', '{value}'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )
    op.execute(
        f"UPDATE settings SET value = '1'::jsonb "
        f"WHERE key = '{LOOK_AHEAD_KEY}' AND value = '2'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE settings SET value = '2'::jsonb "
        f"WHERE key = '{LOOK_AHEAD_KEY}' AND value = '1'::jsonb"
    )
    keys = ", ".join(f"'{key}'" for key in NEW_KEYS)
    op.execute(f"DELETE FROM settings WHERE key IN ({keys})")
