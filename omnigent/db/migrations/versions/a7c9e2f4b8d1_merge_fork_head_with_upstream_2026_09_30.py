"""merge fork host-permissions head with upstream 2026-09-30

Revision ID: a7c9e2f4b8d1
Revises: f2fe7596a89b, ll1a2b3c4d5e
Create Date: 2026-09-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a7c9e2f4b8d1"
down_revision: str | Sequence[str] | None = ("f2fe7596a89b", "ll1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
