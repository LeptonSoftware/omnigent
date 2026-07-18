"""Add host_permissions table for shared team hosts

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-07-17

Backs shared team hosts: one person registers a machine (``omnigent host``)
and grants teammates access to run sessions on it. One row per
``(host_id, user_id)`` grant.

Ownership is not represented here — it stays ``hosts.owner`` and the owner is
implicitly allowed, so a grant row only ever widens access to a non-owner and
revoking is a plain delete. The table is brand-new and is created at the
current schema state, so it carries the tenant-partition ``workspace_id``
column as the leading primary-key member (matching every other table after
``r1a2b3c4d5e6``). There is no foreign-key constraint on ``host_id`` (schema
Rule R032 — see ``p1a2b3c4d5e6``): the relationship to ``hosts`` is enforced
by the application, not the database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the host_permissions table and its lookup index."""
    op.create_table(
        "host_permissions",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        # Relates to hosts.host_id, which is a 16-byte binary UUID (Uuid16).
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(256), nullable=False),
        # Access level as a stable int code (see omnigent.db.enum_codecs
        # HOST_PERMISSION_LEVEL: read=1, use=2).
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "host_id", "user_id"),
        sa.CheckConstraint("level IN (1, 2)", name="ck_host_permissions_level"),
    )
    # The host picker resolves a user's shared hosts on every load
    # (WHERE workspace_id = ? AND user_id = ?).
    op.create_index(
        "ix_host_permissions_user_id",
        "host_permissions",
        ["workspace_id", "user_id", "host_id"],
    )


def downgrade() -> None:
    """Drop the host_permissions table and its index."""
    op.drop_index("ix_host_permissions_user_id", table_name="host_permissions")
    op.drop_table("host_permissions")
