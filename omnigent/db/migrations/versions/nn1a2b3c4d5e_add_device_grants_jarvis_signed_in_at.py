"""add jarvis_signed_in_at to device_grants

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-30 00:00:00.000000

fork: adds a nullable ``jarvis_signed_in_at`` (INTEGER, epoch seconds) to
``device_grants``. Only the Jarvis web sign-in's login grant sets it; every
existing row keeps ``NULL``. So a grant made before this revision can never
pass as a Jarvis sign-in, even one a device client created with
``client_id = 'jarvis'`` while that name was still accepted.

Deployment: additive; older application code ignores the column. Roll back by
downgrading this revision; Jarvis sign-ins made since then
refresh as ordinary (scoped, Omnigent-only) device grants afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMN = "jarvis_signed_in_at"


def _has_column() -> bool:
    """Whether device_grants already has the column: a retried upgrade (a
    database stamped back below this revision) must not add it twice."""
    columns = sa.inspect(op.get_bind()).get_columns("device_grants")
    return _COLUMN in {column["name"] for column in columns}


def upgrade() -> None:
    """Add jarvis_signed_in_at to device_grants, NULL on every existing row."""
    if _has_column():
        return
    op.add_column("device_grants", sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove jarvis_signed_in_at from device_grants."""
    if not _has_column():
        return
    with op.batch_alter_table("device_grants") as batch_op:
        batch_op.drop_column(_COLUMN)
