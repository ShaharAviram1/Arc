"""m15 key art, credits and episode stills

Three columns the redesigned Show and Watch Now pages render instead of
placeholders (M15, architecture.md §4).

``anime.cover_large_url`` is AniList's ``coverImage.extraLarge``. ``cover_url``
already prefers it when AniList answered, but a row filled from MAL during an
outage carries ``main_picture.large`` there — 230 px, visibly soft on a 172 px
card — so the big one gets a column of its own and the client falls back.

``anime.credits`` is ``[{role, name}]`` for the "Made by" block: the studio
first, then whichever of Director / Series Composition / Character Design /
Music / Original Creator AniList's staff connection carries. JSONB because it
is read whole and never queried. MAL publishes no staff at all, so a MAL-filled
row's list is the studio row alone.

``episodes.still_url`` is AniList's ``streamingEpisodes.thumbnail``, matched to
an episode by the "Episode N - Title" pattern in the entry's title; the same
fetch fills ``episodes.title`` where it is null.

All three are nullable and **not backfilled**. Every row fills in on its next
catalogue refresh — the daily sweep covers followed and releasing shows, and
anything else is refreshed the first time somebody opens it — and each column
renders as the placeholder it renders today until then. A backfill would mean
one upstream detail fetch per cached row against a 30/min budget, for artwork
nobody is looking at yet.

Revision ID: 276167cbca27
Revises: 95d81ee797af
Create Date: 2026-09-11 13:21:11.782985

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "276167cbca27"
down_revision: Union[str, Sequence[str], None] = "95d81ee797af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("anime", sa.Column("cover_large_url", sa.Text(), nullable=True))
    op.add_column(
        "anime",
        sa.Column("credits", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("episodes", sa.Column("still_url", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("episodes", "still_url")
    op.drop_column("anime", "credits")
    op.drop_column("anime", "cover_large_url")
