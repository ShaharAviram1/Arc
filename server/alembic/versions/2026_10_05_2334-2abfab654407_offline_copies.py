"""offline_copies, and the offline_idle_days setting

The small offline copy of an episode (FR-P6, M19 T1, owner 2026-10-05): one
row per episode, keyed on it, for the single MP4 at
``DATA_DIR/offline/<episode_id>.mp4`` that "Keep offline" downloads instead of
the full-size ``episode.mp4``. No path column — the file is derived from the
id, like a rendition's directory — and no progress column: progress lives in
the ``offline_encode`` job's payload, as a transcode's does.

``state`` is a varchar like every other enum in Arc (``native_enum=False``), so
a fifth state later is a code change and not DDL. ``episode_id`` cascades: a
copy of an episode that no longer exists is nothing. ``media_file_id`` is set
null when its source goes, which is the ordinary fate of a source.

Also seeds ``offline_idle_days`` = 7 (FR-D2): how long a ready episode's copy
may go unfetched before retention deletes it. In this revision rather than a
data-only one of its own because it is the same feature landing at the same
time — ``9c4a17e5db20``, ``5e0a92c7b431``, ``7c1d5b3ae4f2`` and ``d81f4b6ca3e7``
seeded keys added *after* their feature. ``ON CONFLICT DO NOTHING`` so a value
already set by hand is kept.

Reviewed by hand: autogenerate's table, unchanged apart from layout; no index
beyond the primary key (every read is by episode id, and the idle sweep's scan
is over a table the size of one household's offline shelf); the seed and its
removal added.

Revision ID: 2abfab654407
Revises: 4e76a09e547c
Create Date: 2026-10-05 23:34:46.162363

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2abfab654407"
down_revision: Union[str, Sequence[str], None] = "4e76a09e547c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IDLE_KEY = "offline_idle_days"
IDLE_DEFAULT = "7"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "offline_copies",
        sa.Column("episode_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "preparing",
                "ready",
                "failed",
                name="offlinecopystate",
                native_enum=False,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("etag", sa.String(length=80), nullable=True),
        sa.Column("codec", sa.String(length=16), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("crf", sa.Integer(), nullable=True),
        sa.Column("audio_bitrate", sa.String(length=16), nullable=True),
        sa.Column("settings_key", sa.String(length=32), nullable=True),
        sa.Column("media_file_id", sa.BigInteger(), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_served_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_offline_copies_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_file_id"],
            ["media_files.id"],
            name=op.f("fk_offline_copies_media_file_id_media_files"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("episode_id", name=op.f("pk_offline_copies")),
    )
    op.execute(
        f"INSERT INTO settings (key, value) VALUES ('{IDLE_KEY}', '{IDLE_DEFAULT}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(f"DELETE FROM settings WHERE key = '{IDLE_KEY}'")
    op.drop_table("offline_copies")
