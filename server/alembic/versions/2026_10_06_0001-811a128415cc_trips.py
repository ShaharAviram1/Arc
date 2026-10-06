"""trips, trip_episodes, wants.trip, and the two trip settings

Trips (FR-A12, M19 T3, owner 2026-10-05): one user's "prepare the next X aired
episodes of this show for a trip". ``trips`` is the request — the numbers it
took, its state and its deadline — and ``trip_episodes`` the per-episode
record of where each copy stands, keyed (trip, episode).

``ux_trips_one_active_per_user`` is a partial unique index on ``user_id``
where ``state = 'active'``: one active trip per user, held by the database
because the create path's own check and a second request can both pass it.
``ix_trip_episodes_episode_id`` serves "which trips hold this episode?". Both
foreign keys of each table cascade: a trip of a deleted user or show, or the
row of a deleted episode, is nothing.

``wants.trip`` is BOOLEAN NOT NULL DEFAULT false. A constant default is stored
in the catalogue since Postgres 11, so adding it does not rewrite ``wants``;
every existing row reads false, which is true of every existing row (no trip
has ever existed).

Also seeds ``trip_max_episodes`` = 50 and ``trip_copy_days`` = 14 (FR-D2), in
this revision for the reason ``2abfab654407`` seeded ``offline_idle_days``: the
same feature, landing at the same time. ``ON CONFLICT DO NOTHING`` keeps a
value already set by hand.

Reviewed by hand: autogenerate's tables, indexes and column, unchanged apart
from layout; the seed and its removal added; the downgrade drops in reverse.

Revision ID: 811a128415cc
Revises: 2abfab654407
Create Date: 2026-10-06 00:01:33.792091

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "811a128415cc"
down_revision: Union[str, Sequence[str], None] = "2abfab654407"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEEDS = {"trip_max_episodes": "50", "trip_copy_days": "14"}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "trips",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("anime_id", sa.BigInteger(), nullable=False),
        sa.Column("first_number", sa.Integer(), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "active",
                "finished",
                "cancelled",
                "expired",
                name="tripstate",
                native_enum=False,
                length=32,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["anime_id"],
            ["anime.id"],
            name=op.f("fk_trips_anime_id_anime"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_trips_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trips")),
    )
    op.create_index(
        "ux_trips_one_active_per_user",
        "trips",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "trip_episodes",
        sa.Column("trip_id", sa.BigInteger(), nullable=False),
        sa.Column("episode_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "delivered",
                "expired",
                "cancelled",
                name="tripepisodestate",
                native_enum=False,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_trip_episodes_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trip_id"],
            ["trips.id"],
            name=op.f("fk_trip_episodes_trip_id_trips"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("trip_id", "episode_id", name=op.f("pk_trip_episodes")),
    )
    op.create_index("ix_trip_episodes_episode_id", "trip_episodes", ["episode_id"], unique=False)
    op.add_column(
        "wants",
        sa.Column("trip", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    for key, value in SEEDS.items():
        op.execute(
            f"INSERT INTO settings (key, value) VALUES ('{key}', '{value}'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )


def downgrade() -> None:
    """Downgrade schema."""
    for key in SEEDS:
        op.execute(f"DELETE FROM settings WHERE key = '{key}'")
    op.drop_column("wants", "trip")
    op.drop_index("ix_trip_episodes_episode_id", table_name="trip_episodes")
    op.drop_table("trip_episodes")
    op.drop_index(
        "ux_trips_one_active_per_user",
        table_name="trips",
        postgresql_where=sa.text("state = 'active'"),
    )
    op.drop_table("trips")
