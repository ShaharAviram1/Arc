"""seed batch_sweep_dry_run = true

M21 (owner 2026-11-03, R7, FR-T1, FR-D2): while true, the sweep of a pack's
leftover (unselected) files only logs what it would delete (architecture.md
§5.7). Seeded on so the log is read before anyone switches it off. Inserted
with ``ON CONFLICT DO NOTHING`` so a value already set by hand is kept (the
pattern of ``a7d2e5c9f318``). The downgrade removes the key.

Data only; no schema change, so autogenerate finds nothing.

Revision ID: a8c3e1f5b960
Revises: f6b2d0e4a859
Create Date: 2026-11-03 12:50:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c3e1f5b960"
down_revision: Union[str, Sequence[str], None] = "f6b2d0e4a859"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "batch_sweep_dry_run"
#: JSON text of the default, stored as ``jsonb`` like every other row.
VALUE = "true"


def upgrade() -> None:
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{KEY}', '{VALUE}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key = '{KEY}'")
