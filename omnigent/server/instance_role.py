"""Per-instance switches for running a second server against a shared DB.

This module is FORK-OWNED. A shadow instance (e.g. a standby on another host,
pointed at the production database) must not fire recurring scheduled tasks,
or every task would run twice.
"""

from __future__ import annotations

import os

DISABLE_SCHEDULER_ENV = "OMNIGENT_DISABLE_SCHEDULER"


def scheduler_disabled_by_env() -> bool:
    """Whether ``OMNIGENT_DISABLE_SCHEDULER`` asks this instance not to arm timers.

    Truthy values: ``1``, ``true``, ``yes``, ``on`` (any case). Unset or
    anything else leaves the scheduler on.
    """
    return os.environ.get(DISABLE_SCHEDULER_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
