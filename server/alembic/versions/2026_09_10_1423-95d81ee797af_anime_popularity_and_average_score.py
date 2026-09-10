"""anime popularity and average_score

Two ranking numbers for the recommendation candidate pool (§5.6, FR-R2). The
pool used to fall back on insertion order, on the argument that the season
sweep writes rows in AniList's ``POPULARITY_DESC`` order — true on the day a
season is first swept and progressively less true afterwards, and no help at
all to the genre source, which has no such order to borrow.

``popularity`` is the number of people with the show on a list (AniList's
``popularity``, MAL's ``num_list_users``); ``average_score`` is the mean score
on AniList's 0–100 scale, with MAL's 0–10 ``mean`` scaled on the way in so one
column holds both.

Nullable, unindexed, and **not backfilled**. Nullable because a source may not
publish either and an unaired show has no score yet; unindexed because both
orderings run over a few hundred rows that other predicates have already
selected; not backfilled because every row fills in on its next catalogue
refresh (the daily sweep touches followed and releasing shows, and anything
else is refreshed when somebody opens it) and ``NULLS LAST`` in both orderings
puts an unranked row exactly where it belongs meanwhile.

Revision ID: 95d81ee797af
Revises: 9c4a17e5db20
Create Date: 2026-09-10 14:23:41.056575

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "95d81ee797af"
down_revision: Union[str, Sequence[str], None] = "9c4a17e5db20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("anime", sa.Column("popularity", sa.Integer(), nullable=True))
    op.add_column("anime", sa.Column("average_score", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("anime", "average_score")
    op.drop_column("anime", "popularity")
