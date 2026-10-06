"""seed the trip_pack_min_seeders setting

Fewest seeders Nyaa must list for a pack before a trip prefers it to singles
(FR-A12, FR-D2, owner incident 2026-10-06). The first real trip on production
took a 16 GB pack listed with four seeders and sat ``stalledDL`` at 0 % while
well-seeded singles of the same show existed; below this figure the trip's
search now falls through to the ordinary single path.

``10`` by default. A migration of its own rather than an edit to
``811a128415cc``'s seed for the reason ``d81f4b6ca3e7`` gives: a database that
has already run that revision would never see a key added to it. ``ON CONFLICT
DO NOTHING`` so a value an admin already set by hand is kept.

Data only; no schema change, so autogenerate still finds nothing.

Revision ID: e4b7c2a91f05
Revises: 811a128415cc
Create Date: 2026-10-06 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b7c2a91f05"
down_revision: Union[str, Sequence[str], None] = "811a128415cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "trip_pack_min_seeders"
DEFAULT = "10"


def upgrade() -> None:
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{KEY}', '{DEFAULT}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM settings WHERE key = '{KEY}'")
