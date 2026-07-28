"""Merge the host_permissions branch back into the upstream chain

Revision ID: 98e5bdbcbe03
Revises: b3c4d5e6f7a8, e1f2a3b4c5d6
Create Date: 2026-07-28

``e1f2a3b4c5d6`` (shared team hosts) was authored off ``d1e2f3a4b5c6`` while
upstream continued from the same parent, leaving two heads. This merge point
joins them; it has no schema effect of its own.

Merging rather than re-parenting ``e1f2a3b4c5d6`` is deliberate. Deployments
already carrying ``e1f2a3b4c5d6`` sit on the fork branch, so re-parenting it
onto the upstream head would make them look up to date while silently skipping
every upstream revision in between. With this merge they instead walk the
upstream branch they are missing and then land here.
"""

from __future__ import annotations

revision: str = "98e5bdbcbe03"
down_revision: tuple[str, ...] | None = ("b3c4d5e6f7a8", "e1f2a3b4c5d6")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """No-op: this revision only joins two branches."""


def downgrade() -> None:
    """No-op: this revision only joins two branches."""
