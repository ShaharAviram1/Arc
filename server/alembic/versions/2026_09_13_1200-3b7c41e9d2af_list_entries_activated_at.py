"""list entries activated at

Dormant imports (FR-A9, owner 2026-09-13). A MyAnimeList import is a baseline,
not a request: the owner's brought 414 wants into being in one afternoon, most
of them shows planned years ago whose releases have no seeders left, and they
held every download slot qBittorrent had while the disk filled with what did
arrive. So a ``watching``/``planned`` entry now generates no wants until the
user has touched that show **in Arc**, with a currently-airing show as the
exception; ``activated_at`` is that moment, and it is never cleared.

**The backfill is the migration's real content**, and it is deliberately
generous. An entry is stamped when ``updated_by = 'arc'`` **or**
``mal_synced_at IS NULL``. The first clause is the obvious one: the row's last
change was Arc's own, so the user has plainly acted here, and an upgrade that
made those dormant would stop fetching shows somebody is watching tonight. The
second catches the shape the first misses — a row a user created in Arc that a
later MyAnimeList re-import then overwrote. ``updated_by`` only remembers the
*last* writer, so such a row looks exactly like an import; ``mal_synced_at``
is null on it, because Arc has never successfully pushed it, and an import
alone does not set that column.

The one shape no column can tell from an import is an Arc-made entry that was
later touched by MAL **and** successfully pushed: ``updated_by = 'mal'`` and
``mal_synced_at`` set. Nothing in the schema distinguishes it, and adding a
column to remember it would be a migration for a question asked once. It goes
dormant, and the cost is one press of "Fetch this show" on the Show page — the
want revives unconditionally on the next tick — which is a far smaller price
than the alternative error in the other direction: 414 dead torrents filling
the volume, which is the failure this whole rule exists to prevent.

Rows a MyAnimeList import genuinely created stay null, which is the point.
Anything on those shows that is already downloading is cancelled with its
partial files by the same reconciliation (``cancel_if_unwanted``); anything
already on disk is retention's, on the ordinary grace period. A user who did
want one of them gets it back by opening the show and pressing "Fetch this
show" — or simply by playing an episode.

Nullable with no server default, because null is a meaningful value here
("nobody has") rather than an absence to be filled in. The downgrade drops the
column; every entry reads as dormant-or-airing again, which is the behaviour of
the code that goes with it.

Revision ID: 3b7c41e9d2af
Revises: 0aa6beaab5ed
Create Date: 2026-09-13 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b7c41e9d2af"
down_revision: Union[str, Sequence[str], None] = "0aa6beaab5ed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "list_entries",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Arc-made entries stay live; MAL-imported ones start dormant (FR-A9).
    # ``mal_synced_at IS NULL`` is the second half: a row Arc created that a
    # later import overwrote has ``updated_by = 'mal'`` but has never been
    # pushed, and must not go dormant.
    op.execute(
        "UPDATE list_entries SET activated_at = updated_at "
        "WHERE updated_by = 'arc' OR mal_synced_at IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("list_entries", "activated_at")
