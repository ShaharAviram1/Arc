"""renditions kind, text subtitles, and the prepare-job index (M20 T2)

Three changes behind the remux (architecture.md §5.3c):

* ``renditions.kind`` — how the rendition was made: ``remux`` (video copied,
  subtitles as a WebVTT track) or ``transcode`` (the burn-in encode, now the
  fallback). Every existing row is a transcode, which is the server default.
* ``renditions.text_subtitles`` — whether the rendition carries a WebVTT
  track. ``false`` for every existing row: a transcode burns its subtitles in.
* ``ix_jobs_transcode_episode`` (partial on ``type = 'transcode'``) becomes
  ``ix_jobs_prepare_episode`` (partial on ``type IN ('transcode', 'remux')``),
  because ``latest_transcode_jobs`` now answers for either prepare job.

Both columns are ``NOT NULL`` with a constant server default: a catalogue-only
change on PostgreSQL 11+, no table rewrite. The indexes are written as SQL, as
the original was, because they are on an expression autogenerate spells
differently on the way back.

**Downgrading loses meaning, not rows.** After a downgrade the older code
does not know remux renditions or ``remux`` jobs: a ``renditions`` row of a
remux reads as a burn-in transcode whose ``index.m3u8`` does not exist (the
media route 404s it until a ``force`` re-encode), and ``remux`` job rows have
no handler (the worker fails them as unknown types). Re-encode the remuxed
episodes after a downgrade, or upgrade again.

Revision ID: caf1811e8ee6
Revises: 603a536051f9
Create Date: 2026-10-13 19:43:27.737058

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "caf1811e8ee6"
down_revision: Union[str, Sequence[str], None] = "603a536051f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_INDEX = "ix_jobs_transcode_episode"
NEW_INDEX = "ix_jobs_prepare_episode"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(f"DROP INDEX IF EXISTS {OLD_INDEX}")
    op.execute(
        f"CREATE INDEX {NEW_INDEX} ON jobs ((payload ->> 'episode_id')) "
        "WHERE type IN ('transcode', 'remux')"
    )
    op.add_column(
        "renditions",
        sa.Column("kind", sa.String(length=16), server_default="transcode", nullable=False),
    )
    op.add_column(
        "renditions",
        sa.Column("text_subtitles", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("renditions", "text_subtitles")
    op.drop_column("renditions", "kind")
    op.execute(f"DROP INDEX IF EXISTS {NEW_INDEX}")
    op.execute(
        f"CREATE INDEX {OLD_INDEX} ON jobs ((payload ->> 'episode_id')) WHERE type = 'transcode'"
    )
