"""seed batch_prefer_ratio, slow_swarm_seeders and look_ahead_slow

Three admin-editable rules (FR-D2, owner 2026-10-20) whose behaviour lands in
follow-up work:

- ``batch_prefer_ratio`` = 3: on a finished show, a season pack whose seeders
  are at least this many times the best single's (and at least 4) is taken file
  by file instead of the single (FR-A4/FR-A11). 0 never prefers a batch.
- ``slow_swarm_seeders`` = 20: a show whose newest fetched release recorded
  fewer seeders than this is "thinly seeded" (FR-A1). 0: no show ever is.
- ``look_ahead_slow`` = 3: how many episodes ahead Arc keeps for a thinly
  seeded show instead of ``look_ahead_n`` (FR-A1).

Inserted with ``ON CONFLICT DO NOTHING`` so a value already set by hand is
kept, as in ``a7d2e5c9f318``. The downgrade removes the three keys.

Data only; no schema change, so autogenerate finds nothing.

Revision ID: c4b8e1f2a9d6
Revises: 244fbc2111f9
Create Date: 2026-10-20 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4b8e1f2a9d6"
down_revision: Union[str, Sequence[str], None] = "244fbc2111f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: key → JSON text of its default, stored as ``jsonb`` like every other row.
NEW_KEYS: dict[str, str] = {
    "batch_prefer_ratio": "3",
    "slow_swarm_seeders": "20",
    "look_ahead_slow": "3",
}


def upgrade() -> None:
    for key, value in NEW_KEYS.items():
        op.execute(
            f"INSERT INTO settings (key, value) VALUES ('{key}', '{value}'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )


def downgrade() -> None:
    keys = ", ".join(f"'{key}'" for key in NEW_KEYS)
    op.execute(f"DELETE FROM settings WHERE key IN ({keys})")
