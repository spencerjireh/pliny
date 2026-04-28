"""items.wayback_fallback_version + index

Adds the version column for the wayback_fallback stage so the runner's
generic `UPDATE items SET {stage}_version = :v` works without
special-casing.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column(
            "wayback_fallback_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "items_wayback_fallback_version_idx",
        "items",
        ["wayback_fallback_version"],
    )


def downgrade() -> None:
    op.drop_index("items_wayback_fallback_version_idx", table_name="items")
    op.drop_column("items", "wayback_fallback_version")
