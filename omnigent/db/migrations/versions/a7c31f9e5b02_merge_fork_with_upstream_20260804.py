"""Merge the fork branch back into the upstream chain

Revision ID: a7c31f9e5b02
Revises: 98e5bdbcbe03, e6f7a8b9c0d1
Create Date: 2026-08-04

Pulling upstream forward left two heads again: the fork's ``98e5bdbcbe03``
(shared team hosts) and upstream's ``e6f7a8b9c0d1``. This merge point joins
them; it has no schema effect of its own.

As with the previous merge, joining rather than re-parenting is deliberate —
deployments already carrying ``98e5bdbcbe03`` walk the upstream revisions they
are missing and then land here, instead of appearing up to date while skipping
them.
"""

from __future__ import annotations

revision: str = "a7c31f9e5b02"
down_revision: tuple[str, ...] | None = ("98e5bdbcbe03", "e6f7a8b9c0d1")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """No-op: this revision only joins two branches."""


def downgrade() -> None:
    """No-op: this revision only joins two branches."""
