"""partial index on watch_progress for a user's unfinished episodes

"Continue watching" (FR-W1) and the home page's resume rail both ask one
question: which episodes has *this* user started and not finished, newest
first? ``ix_watch_progress_user_id_updated_at`` covers the user and the order
but not the filter, so the answer costs a scan of every row that user has ever
written — and completion is sticky (FR-S4), so after a season of watching the
finished rows are almost all of them.

This index is on ``(user_id, updated_at DESC)`` and partial on
``completed = false``. Partial because the excluded rows are excluded forever:
the index stays the size of what somebody is part-way through rather than of
everything they have watched. ``DESC`` because that is the direction the query
reads, and a descending scan of an ascending index is a sort Postgres does not
have to do.

Created with the raw statement rather than ``op.create_index``: the ordering
and the predicate are written out here exactly as
``arc.models.tracking.IN_PROGRESS_EXPRESSION`` and ``IN_PROGRESS_PREDICATE``
spell them, the same way ``ix_jobs_transcode_episode`` is — a migration is a
historical record, and it keeps applying the same way after those constants
move on.

Revision ID: 6e2f1a8b93c4
Revises: 8b0f2a7c41d9
Create Date: 2026-09-07 09:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6e2f1a8b93c4"
down_revision: Union[str, Sequence[str], None] = "8b0f2a7c41d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_watch_progress_in_progress"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX {INDEX_NAME} ON watch_progress (user_id, updated_at DESC) "
        "WHERE completed = false"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {INDEX_NAME}")
