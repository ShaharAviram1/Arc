"""verified delivery: offline_copies.sha256, holdings detail, device_reports

A held copy is never destroyed, and "delivered" means verified (owner,
2026-10-30, after episode 15 of trip 4 was lost mid-flight):

* ``offline_copies.sha256`` — the finished copy's SHA-256 (lower-case hex),
  computed by the encode before the row goes ``ready``. Null for older copies:
  no backfill (the device skips the hash check when the server has none).
* ``device_holdings.etag``, ``.size``, ``.sha256`` — what the device says it
  holds of each episode's copy, when its report carries it (null for a report
  in the old ids-only shape).
* ``device_reports`` — a device's own event log, posted with "Send report"
  for the operator (``arc.cli device-report``); at most twenty per device.

Every new column is nullable with no default: catalogue-only on PostgreSQL,
no table rewrite.

Revision ID: a7c2e4f9d1b3
Revises: 3f7a1c9e5b42
Create Date: 2026-10-30 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7c2e4f9d1b3"
down_revision: Union[str, Sequence[str], None] = "3f7a1c9e5b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("offline_copies", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.add_column("device_holdings", sa.Column("etag", sa.String(length=200), nullable=True))
    op.add_column("device_holdings", sa.Column("size", sa.BigInteger(), nullable=True))
    op.add_column("device_holdings", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.create_table(
        "device_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_device_reports_device_id_received_at",
        "device_reports",
        ["device_id", "received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_device_reports_user_id"), "device_reports", ["user_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the report table and the three holdings columns and the copy's
    hash: confirmations go back to trusting the length alone, as the code
    before this revision did.
    """
    op.drop_index(op.f("ix_device_reports_user_id"), table_name="device_reports")
    op.drop_index("ix_device_reports_device_id_received_at", table_name="device_reports")
    op.drop_table("device_reports")
    op.drop_column("device_holdings", "sha256")
    op.drop_column("device_holdings", "size")
    op.drop_column("device_holdings", "etag")
    op.drop_column("offline_copies", "sha256")
