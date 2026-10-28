"""trips: one active trip per show per user, not per user

The owner's decision of 2026-10-28: a user may have any number of active
trips, at most one per show; the device's free space is the only other limit.
``ux_trips_one_active_per_user`` (``user_id`` where ``state = 'active'``,
revision ``811a128415cc``) is replaced by ``ux_trips_one_active_per_show`` on
``(user_id, anime_id)`` with the same predicate.

The downgrade puts the per-user index back, which cannot be built while some
user has more than one active trip. It refuses with a clear error naming them
rather than choosing which trips to end: cancelling a trip is a user's act
(FR-A12), not a migration's.

Revision ID: 5d3e9a7c1b20
Revises: c4b8e1f2a9d6
Create Date: 2026-10-28 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d3e9a7c1b20"
down_revision: Union[str, Sequence[str], None] = "c4b8e1f2a9d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_INDEX = "ux_trips_one_active_per_user"
NEW_INDEX = "ux_trips_one_active_per_show"
PREDICATE = "state = 'active'"


def upgrade() -> None:
    """Swap the per-user index for the per-show one."""
    op.drop_index(OLD_INDEX, table_name="trips", postgresql_where=sa.text(PREDICATE))
    op.create_index(
        NEW_INDEX,
        "trips",
        ["user_id", "anime_id"],
        unique=True,
        postgresql_where=sa.text(PREDICATE),
    )


def downgrade() -> None:
    """Back to one active trip per user; refused while a user has several."""
    crowded = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT user_id FROM trips WHERE state = 'active' "
                "GROUP BY user_id HAVING count(*) > 1 ORDER BY user_id"
            )
        )
        .scalars()
        .all()
    )
    if crowded:
        raise RuntimeError(
            "cannot downgrade below 5d3e9a7c1b20: users "
            f"{', '.join(str(user_id) for user_id in crowded)} have more than one "
            "active trip; cancel all but one of each user's trips first"
        )
    op.drop_index(NEW_INDEX, table_name="trips", postgresql_where=sa.text(PREDICATE))
    op.create_index(
        OLD_INDEX,
        "trips",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(PREDICATE),
    )
