"""partial expression index on jobs for a transcode's episode

``latest_transcode_jobs`` asks "which is the newest transcode job for each of
these episode ids?" on every show page and every home page, and a transcode
records its episode in ``jobs.payload`` rather than in a column of its own
(see ``arc.services.media.jobs`` for why). Without an index that lookup is a
sequential scan of the whole queue, and the queue is the one table that only
ever grows.

The index is on the *expression* ``payload ->> 'episode_id'`` and is partial on
``type = 'transcode'``: no other job type carries that key, so restricting it
keeps the index a fraction of the table's size and lets Postgres use it for the
``DISTINCT ON`` that picks the newest row per episode.

Created with the raw statement rather than ``op.create_index``: the expression
and the predicate are written out here exactly as
``arc.models.job.TRANSCODE_EPISODE_EXPRESSION`` and
``TRANSCODE_EPISODE_PREDICATE`` spell them, and a migration is a historical
record — it keeps applying the same way after those constants move on.

Revision ID: 8b0f2a7c41d9
Revises: 4d1c3479c036
Create Date: 2026-09-06 11:30:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b0f2a7c41d9"
down_revision: Union[str, Sequence[str], None] = "4d1c3479c036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_jobs_transcode_episode"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX {INDEX_NAME} ON jobs ((payload ->> 'episode_id')) "
        "WHERE type = 'transcode'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {INDEX_NAME}")
