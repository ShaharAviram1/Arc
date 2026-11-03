"""seed demand_max_open = 3

M21 (owner 2026-11-03, R4, FR-A14, FR-D2): how many episodes one user may have
fetching on request at once; 0 removes the cap, like ``slot_cap_k``. Inserted
with ``ON CONFLICT DO NOTHING`` so a value already set by hand is kept (the
pattern of ``a7d2e5c9f318``). The downgrade removes the key.

Data only; no schema change, so autogenerate finds nothing.

Revision ID: e5a1c9d3f748
Revises: d4f8b2c6e937
Create Date: 2026-11-03 12:30:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a1c9d3f748"
down_revision: Union[str, Sequence[str], None] = "d4f8b2c6e937"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "demand_max_open"
#: JSON text of the default, stored as ``jsonb`` like every other row.
VALUE = "3"


def upgrade() -> None:
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{KEY}', '{VALUE}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key = '{KEY}'")
