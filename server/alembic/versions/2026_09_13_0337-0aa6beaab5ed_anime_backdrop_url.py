"""anime backdrop url

TMDB's 16:9 backdrop, in a column of its own (owner, 2026-09-13). AniList's
``banner_url`` is a 1900×400 strip and the M15 design never shows one as a
picture — the 21:9 hero rejects a banner wider than 2.6:1, the 16:9 episode
card one wider than 2.2:1 — so a show AniList reached first sat on the
blurred-poster wash for good. Sharing ``banner_url`` could not fix it: the TMDB
enrichment may only fill that column where it is null (cache rule 3), and an
AniList refresh would write the strip back over it anyway.

Null on every existing row, and only the TMDB enrichment ever writes it. No
backfill: a null backdrop on a mapped show is a hole the nightly sweep already
looks for (``tmdb.jobs._missing_key_art``), so the rows fill in over the next
few nights at :data:`~arc.services.tmdb.jobs.SWEEP_LIMIT` shows a night, and a
deployment with no ``TMDB_API_KEY`` keeps rendering AniList's art as before.

Revision ID: 0aa6beaab5ed
Revises: 73f2f8eac7e7
Create Date: 2026-09-13 03:37:28.459004

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0aa6beaab5ed"
down_revision: Union[str, Sequence[str], None] = "73f2f8eac7e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("anime", sa.Column("backdrop_url", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("anime", "backdrop_url")
