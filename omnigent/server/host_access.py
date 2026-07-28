"""Shared-team-host authorization — the fork's own extension point.

This module is FORK-OWNED. Upstream has no file here, so it can never conflict
on a merge. Everything the shared-hosts feature needs to decide "may this caller
reach this host?" lives here, so the edits inside upstream-owned files stay
one-liners (a swapped condition, an import) instead of rewritten blocks.

Two questions, deliberately kept apart:

- :func:`host_access_allowed` / :func:`resolve_host_access` — may the caller
  REACH this host (launch a session, browse files)? True for the owner, and for
  a user the owner granted at least the required level.
- Ownership itself (delete the host, revoke its launch token, manage its shares)
  stays with upstream's ``resolve_host_owner``. A grant never confers those: a
  teammate you let run code on your laptop must not be able to delete or
  re-share it.

A ``use`` grant is code execution on the owner's machine as the owner's OS user.
It is not a sandbox — the grant is the whole trust decision.
"""

from __future__ import annotations

from fastapi import HTTPException

from omnigent.db.enum_codecs import encode_host_permission_level
from omnigent.stores.host_store import HOST_LEVEL_USE, Host, HostStore


def host_access_allowed(
    host: Host,
    user_id: str | None,
    host_store: HostStore,
    required_level: str = HOST_LEVEL_USE,
) -> bool:
    """Return whether *user_id* may reach *host* at *required_level*.

    Written as a plain predicate so upstream call sites need only swap the
    condition in their existing owner check, leaving the surrounding
    fetch/404/403 shape untouched.

    :param host: The already-loaded host record.
    :param user_id: Authenticated caller, or ``None`` when auth is disabled
        (single-user/local), where the check is skipped.
    :param host_store: Store used to look up an access grant.
    :param required_level: ``"read"`` (see the host and its metadata) or
        ``"use"`` (default; launch sessions, browse files).
    :returns: ``True`` when the caller owns the host or holds a sufficient grant.
    """
    if user_id is None or host.user_id == user_id:
        return True
    granted = host_store.get_host_access_level(host.host_id, user_id)
    if granted is None:
        return False
    # Levels are ordered by privilege, so "at least" is a code comparison.
    return encode_host_permission_level(granted) >= encode_host_permission_level(required_level)


def resolve_host_access(
    *,
    user_id: str | None,
    host_id: str,
    host_store: HostStore,
    required_level: str = HOST_LEVEL_USE,
) -> Host:
    """Load a host and authorize that the caller may reach it.

    The resolver form, for paths that must authorize BEFORE touching the host —
    notably the runner launch and the session-create workspace probe, which
    sends a ``host.stat``. The original bug had that probe contacting another
    user's host before any check ran.

    :param user_id: Authenticated caller, e.g. ``"bob@example.com"``, or
        ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param host_store: Persistent host registrations.
    :param required_level: Minimum access needed (``"read"`` / ``"use"``).
    :returns: The host record the caller may reach.
    :raises HTTPException: 404 if the host is unknown (existence is not leaked);
        403 if the caller neither owns it nor holds a sufficient grant.
    """
    host = host_store.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    if not host_access_allowed(host, user_id, host_store, required_level):
        raise HTTPException(status_code=403, detail="not your host")
    return host
