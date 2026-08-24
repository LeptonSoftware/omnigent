"""merge fork host-permissions head with upstream 2026-08-24

Revision ID: f2fe7596a89b
Revises: 98e5bdbcbe03, e5d9bc8ac650
Create Date: 2026-08-24 09:56:28.037988
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f2fe7596a89b"
down_revision: str | Sequence[str] | None = ("98e5bdbcbe03", "e5d9bc8ac650")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
