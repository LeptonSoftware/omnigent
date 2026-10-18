"""Per-instance switches for running several servers against a shared DB.

This module is FORK-OWNED. A shadow instance (e.g. a standby on another host,
pointed at the production database) must not fire recurring scheduled tasks,
or every task would run twice. Replicas behind a host-keyed router also need
``create_app(host_sharded=True)``, which the Docker entrypoint reads from
``OMNIGENT_HOST_SHARDED``.
"""

from __future__ import annotations

import os

DISABLE_SCHEDULER_ENV = "OMNIGENT_DISABLE_SCHEDULER"
HOST_SHARDED_ENV = "OMNIGENT_HOST_SHARDED"
_TRUTHY = {"1", "true", "yes", "on"}


def scheduler_disabled_by_env() -> bool:
    """Whether ``OMNIGENT_DISABLE_SCHEDULER`` asks this instance not to arm timers.

    Truthy values: ``1``, ``true``, ``yes``, ``on`` (any case). Unset or
    anything else leaves the scheduler on.
    """
    return os.environ.get(DISABLE_SCHEDULER_ENV, "").strip().lower() in _TRUTHY


def host_sharded_by_env() -> bool:
    """Whether ``OMNIGENT_HOST_SHARDED`` says replicas sit behind a host-keyed router.

    Same truthy values as :func:`scheduler_disabled_by_env`. When on, a request
    for a live host whose tunnel is on another replica gets 400
    ``wrong_replica`` instead of 409 "host is offline".
    """
    return os.environ.get(HOST_SHARDED_ENV, "").strip().lower() in _TRUTHY
