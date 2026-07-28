"""
Persistent store for host registrations.

Hosts are machines connected via ``omnigent host``. The store
tracks which hosts have ever connected, their names, user_ids, and
online/offline status. The ``hosts`` table is the source of truth
for ``GET /v1/hosts`` — all server replicas query it. Live WebSocket
connection state is tracked separately in the in-memory
``HostRegistry`` (one per replica).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

from sqlalchemy import Engine, or_, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlHost,
    SqlHostPermission,
    current_workspace_id,
)
from omnigent.db.enum_codecs import (
    decode_host_permission_level,
    decode_host_status,
    encode_host_permission_level,
    encode_host_status,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.harness_availability import HarnessAvailability, is_harness_availability

# A host is considered live only if its row was touched (connect or
# heartbeat) within this window. The host tunnel's ping loop writes a
# heartbeat every PING_INTERVAL_S (30s); three missed heartbeats means
# the host is gone. This freshness gate is the safety net for every
# path that never runs set_offline — hard crash, OOM, deploy/replica
# restart, silent network drop, or a connect that died after the online
# upsert. It must stay >= the tunnel's ping-miss window
# (PING_INTERVAL_S * PING_MISS_THRESHOLD) so a healthy host that is
# still heart-beating is never falsely aged out.
HOST_LIVENESS_TTL_S = 90


@dataclass
class Host:
    """
    A registered host machine.

    :param host_id: Stable identifier from the host's local
        ``~/.omnigent/config.yaml``, e.g. ``"host_a1b2c3d4..."``.
    :param name: Human-readable name, e.g. ``"corey-laptop"``.
    :param user_id: User ID from the Databricks auth Bearer token,
        e.g. ``"corey.zumar@databricks.com"``.
    :param status: ``"online"`` or ``"offline"``.
    :param created_at: Unix epoch seconds of first registration.
    :param updated_at: Unix epoch seconds the row was last touched —
        a status change (connect/disconnect) or a tunnel heartbeat.
        Used as the host's last-seen for the liveness freshness gate
        (see :data:`HOST_LIVENESS_TTL_S`).
    :param sandbox_provider: Sandbox provider backing a SERVER-MANAGED
        host (``host_type="managed"`` sessions), e.g. ``"modal"``.
        ``None`` for external (user-connected) hosts — non-``None``
        marks the host as server-managed.
    :param sandbox_id: Provider-assigned id of the sandbox currently
        backing a managed host, e.g. ``"sb-a1b2c3"`` — what
        termination is issued against. ``None`` for external hosts.
    :param configured_harnesses: Per-harness readiness reported in the
        host's last ``host.hello`` frame, e.g.
        ``{"claude-sdk": True, "codex": False}``. ``None`` when the
        host has never reported it (older host build) — unknown, not
        "nothing configured".
    """

    host_id: str
    name: str
    user_id: str
    status: str
    created_at: int
    updated_at: int
    sandbox_provider: str | None = None
    sandbox_id: str | None = None
    configured_harnesses: dict[str, HarnessAvailability] | None = None


@dataclass
class HostGrant:
    """
    An access grant letting a non-owner use someone else's host.

    Ownership is never a grant — it stays ``Host.user_id`` and is always
    implicitly allowed, so a grant only ever widens access.

    :param host_id: The shared host, e.g. ``"host_a1b2c3d4..."``.
    :param user_id: The grantee, e.g. ``"bob@example.com"``.
    :param level: ``"read"`` (visible in the picker, metadata readable)
        or ``"use"`` (additionally launch sessions and browse files).
    :param created_at: Unix epoch seconds the grant was made.
    """

    host_id: str
    user_id: str
    level: str
    created_at: int


# Access levels, ordered by privilege — compare codes to test "at least".
HOST_LEVEL_READ = "read"
HOST_LEVEL_USE = "use"


def _row_to_grant(row: SqlHostPermission) -> HostGrant:
    """
    Convert a :class:`SqlHostPermission` ORM row to a :class:`HostGrant`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`HostGrant` dataclass instance.
    """
    return HostGrant(
        host_id=row.host_id,
        user_id=row.user_id,
        level=decode_host_permission_level(row.level),
        created_at=row.created_at,
    )


def host_is_live(host: Host, now: int | None = None) -> bool:
    """
    Return whether a :class:`Host` is online and recently seen.

    Pure helper over an already-loaded entity (no DB access), so
    callers that already hold a :class:`Host` — or a list of them —
    don't re-query per row. A host is live only when its ``status`` is
    ``"online"`` **and** its last-seen (``updated_at``) is within
    :data:`HOST_LIVENESS_TTL_S`; the freshness half is what catches a
    host that died without a graceful disconnect.

    :param host: The host entity to evaluate.
    :param now: Unix epoch seconds to measure freshness against;
        defaults to the current time. Pass an explicit value to
        classify many hosts against one consistent clock.
    :returns: ``True`` when the host is online and fresh.
    """
    ref = now if now is not None else now_epoch()
    return host.status == "online" and host.updated_at >= ref - HOST_LIVENESS_TTL_S


_logger = logging.getLogger(__name__)


def _parse_configured_harnesses(raw: str | None) -> dict[str, HarnessAvailability] | None:
    """
    Parse the JSON-encoded ``hosts.configured_harnesses`` column.

    Tolerant: ``NULL``, malformed JSON, or a non-object payload all
    map to ``None`` ("unknown") — a corrupt column value must degrade
    to no-warning in the UI, never break host listing. Entries with a
    unsupported readiness value are dropped for the same reason.

    :param raw: The raw column value, e.g.
        ``'{"claude-sdk": true, "codex": false}'`` or ``None``.
    :returns: The readiness map, or ``None`` when absent or unparseable.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("Ignoring malformed hosts.configured_harnesses value")
        return None
    if not isinstance(parsed, dict):
        return None
    return {k: v for k, v in parsed.items() if isinstance(k, str) and is_harness_availability(v)}


def _row_to_host(row: SqlHost) -> Host:
    """
    Convert a :class:`SqlHost` ORM row to a :class:`Host` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Host` dataclass instance.
    """
    return Host(
        host_id=row.host_id,
        name=row.name,
        user_id=row.user_id,
        status=decode_host_status(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        sandbox_provider=row.sandbox_provider,
        sandbox_id=row.sandbox_id,
        configured_harnesses=_parse_configured_harnesses(row.configured_harnesses),
    )


def hash_host_launch_token(token: str) -> str:
    """
    Digest a managed-host launch token for storage / lookup.

    Only the digest is ever persisted (``hosts.token_hash``), so a
    database leak does not leak usable credentials, and the
    tunnel-side lookup is by digest — the raw token never touches a
    query.

    :param token: The raw launch token, e.g. the value of
        ``secrets.token_urlsafe(32)``.
    :returns: Hex SHA-256 digest, e.g. ``"9f86d08..."`` (64 chars).
    """
    return hashlib.sha256(token.encode()).hexdigest()


class HostStore:
    """
    Persistent store for host registrations backed by SQLAlchemy.

    :param storage_location: SQLAlchemy database URI, e.g.
        ``"sqlite:///hosts.db"``.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the host store.

        :param storage_location: SQLAlchemy database URI, e.g.
            ``"sqlite:///hosts.db"``.
        """
        self._engine: Engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def upsert_on_connect(
        self,
        host_id: str,
        name: str,
        user_id: str,
        *,
        allow_host_id_reown: bool = False,
        configured_harnesses: dict[str, HarnessAvailability] | None = None,
    ) -> Host:
        """
        Register or update a host on WebSocket connect.

        Inserts a new row if ``host_id`` does not exist, otherwise
        updates ``name``, ``user_id``, ``status``, and ``updated_at``.
        Called by the host tunnel endpoint when a host sends its
        ``host.hello`` frame.

        The upsert keys on the ``(user_id, name)`` primary key, but
        ``host_id`` carries its own UNIQUE constraint. When the same
        physical host re-registers under a *different* user_id (e.g. a
        local server respawned with a flipped auth posture changes the
        user_id between an accounts user and the reserved ``local`` user),
        the ``(user_id, name)`` lookup misses and a plain INSERT would
        collide on ``host_id``. That collision is a deliberate W2-class
        boundary in shared deployments — a different user must not be
        able to claim another user's host_id — so re-owning is gated
        behind *allow_host_id_reown*, which the server sets only for the
        loopback single-user local server. Remote / multi-user servers
        never set it, so the hijack boundary stays intact (the INSERT
        raises ``IntegrityError`` and fails the handshake closed).

        :param host_id: Stable host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param name: Human-readable name from ``config.yaml``, e.g.
            ``"corey-laptop"``.
        :param user_id: Authenticated user ID from the Bearer token,
            e.g. ``"corey.zumar@databricks.com"``.
        :param allow_host_id_reown: When ``True`` and a row already
            exists for *host_id* under a different ``(user_id, name)``,
            re-own that row in place (preserving the ``host_id`` and its
            conversation bindings) instead of inserting. Intended solely
            for the single-user loopback local server.
        :param configured_harnesses: Per-harness readiness from the
            host's ``host.hello`` frame, e.g. ``{"claude-sdk": True}``.
            Written on every connect — including ``None`` from an older
            host that doesn't report it, which correctly resets any
            stale value back to "unknown".
        :returns: The upserted :class:`Host`.
        """
        now = now_epoch()
        harnesses_json = (
            json.dumps(configured_harnesses) if configured_harnesses is not None else None
        )
        with self._session() as session:
            # Primary lookup: by (workspace_id, host_id) — the new PK.
            row = session.get(SqlHost, (current_workspace_id(), host_id))
            if row is not None:
                # W2-class boundary: a different user must not claim another
                # user's host_id. Raise the same IntegrityError the old UNIQUE
                # constraint produced so the tunnel handler rejects the hijack.
                if row.user_id != user_id and not allow_host_id_reown:
                    raise IntegrityError(
                        "host_id already owned by a different user",
                        params={"host_id": host_id, "user_id": user_id},
                        orig=Exception("UNIQUE constraint failed: hosts.host_id"),
                    )
                # Known host_id (same user_id, or reown opted in): update
                # user_id/name in case they changed, then refresh status and timestamp.
                if row.user_id != user_id:
                    # Ownership changed: the previous owner's sharing
                    # decisions don't bind the new one, so drop the
                    # grants rather than silently carrying a teammate's
                    # access onto a machine that changed hands.
                    session.execute(
                        sql_delete(SqlHostPermission).where(
                            SqlHostPermission.workspace_id == current_workspace_id(),
                            SqlHostPermission.host_id == host_id,
                        )
                    )
                row.user_id = user_id
                row.name = name
                row.status = encode_host_status("online")
                row.updated_at = now
                row.configured_harnesses = harnesses_json
                return _row_to_host(row)

            # host_id is new — check whether (workspace_id, user_id, name)
            # already exists. If it does, the same machine regenerated its
            # identity file: this is a host_id rotation. If allow_host_id_reown
            # is set, also check if any row holds this host_id under a different
            # user_id and re-own it instead of inserting.
            if allow_host_id_reown:
                reowned = self._reown_host_id(
                    session,
                    host_id=host_id,
                    name=name,
                    user_id=user_id,
                    configured_harnesses_json=harnesses_json,
                )
                if reowned is not None:
                    return reowned

            existing_by_name = session.execute(
                select(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.user_id == user_id,
                    SqlHost.name == name,
                )
            ).scalar_one_or_none()
            if existing_by_name is not None:
                # Same (user_id, name), different host_id: identity rotation.
                # host_id is now part of the PK, so we can't UPDATE it via the
                # ORM — delete the old row and insert a fresh one that carries
                # the new host_id while preserving created_at.
                row = self._rotate_host_id(session, existing_by_name, host_id, now, harnesses_json)
                return _row_to_host(row)

            # Genuinely new host: plain INSERT.
            row = SqlHost(
                user_id=user_id,
                name=name,
                host_id=host_id,
                status=encode_host_status("online"),
                created_at=now,
                updated_at=now,
                configured_harnesses=harnesses_json,
            )
            session.add(row)
            return _row_to_host(row)

    @staticmethod
    def _rotate_host_id(
        session: Session,
        row: SqlHost,
        new_host_id: str,
        now: int,
        harnesses_json: str | None,
    ) -> SqlHost:
        """Replace a host row's host_id while repointing its conversations.

        ``host_id`` is now part of the PK, so an in-place UPDATE is not
        possible via the ORM. The rotation is:

        1. Capture the conversation ids bound to the old host_id.
        2. NULL them so nothing references the old PK value.
        3. DELETE the old row (host_id was the PK member being changed).
        4. INSERT a new row with the new host_id, preserving ``created_at``.
        5. Reattach the captured conversations to the new host_id.

        All steps run inside the caller's transaction so a failure rolls
        the whole upsert back.

        :param session: The active SQLAlchemy session.
        :param row: The existing host row whose ``host_id`` rotates.
        :param new_host_id: The host_id the host reconnected with.
        :param now: Unix epoch seconds for the updated_at timestamp.
        :param harnesses_json: JSON-encoded harness readiness, or None.
        :returns: The newly inserted :class:`SqlHost` row.
        """
        old_host_id = row.host_id
        # Preserve durable fields from the outgoing row before deletion.
        created_at = row.created_at
        user_id = row.user_id
        name = row.name
        token_hash = row.token_hash
        token_expires_at = row.token_expires_at
        sandbox_provider = row.sandbox_provider
        sandbox_id = row.sandbox_id

        bound_ids = list(
            session.execute(
                select(SqlConversationMetadata.id).where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.host_id == old_host_id,
                )
            ).scalars()
        )
        if bound_ids:
            session.execute(
                update(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.host_id == old_host_id,
                )
                .values(host_id=None)
            )
            session.flush()

        # Delete the old PK row and insert a new one with the rotated host_id.
        session.execute(
            sql_delete(SqlHost).where(
                SqlHost.workspace_id == current_workspace_id(),
                SqlHost.host_id == old_host_id,
            )
        )
        session.flush()

        new_row = SqlHost(
            workspace_id=current_workspace_id(),
            host_id=new_host_id,
            user_id=user_id,
            name=name,
            status=encode_host_status("online"),
            created_at=created_at,
            updated_at=now,
            token_hash=token_hash,
            token_expires_at=token_expires_at,
            sandbox_provider=sandbox_provider,
            sandbox_id=sandbox_id,
            configured_harnesses=harnesses_json,
        )
        session.add(new_row)
        session.flush()

        # Repoint access grants at the rotated host_id. It is the same
        # physical machine under a new id, so a teammate's access must
        # survive the rotation rather than silently disappearing.
        session.execute(
            update(SqlHostPermission)
            .where(
                SqlHostPermission.workspace_id == current_workspace_id(),
                SqlHostPermission.host_id == old_host_id,
            )
            .values(host_id=new_host_id)
        )
        session.flush()

        if bound_ids:
            session.execute(
                update(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id.in_(bound_ids),
                )
                .values(host_id=new_host_id)
            )
            session.flush()

        return new_row

    def _reown_host_id(
        self,
        session: Session,
        *,
        host_id: str,
        name: str,
        user_id: str,
        configured_harnesses_json: str | None = None,
    ) -> Host | None:
        """Re-own an existing host_id row under a new ``(user_id, name)``.

        Used only when ``upsert_on_connect`` opts in via
        ``allow_host_id_reown`` (the single-user loopback local server).
        Updates ``user_id``, ``name``, ``status``, and ``updated_at`` on the
        row that already holds *host_id*, leaving ``host_id`` itself
        unchanged so the ``conversations.host_id`` foreign-key bindings
        survive the user_id change. ``(workspace_id, user_id, name)`` is a
        unique constraint (the PK is ``(workspace_id, host_id)``), so the
        change is issued as a Core ``UPDATE`` rather than loading and
        mutating the ORM object in place.

        :param session: The active SQLAlchemy session.
        :param host_id: Host identifier whose row should be re-owned,
            e.g. ``"host_a1b2c3d4..."``.
        :param name: New host name to record, e.g. ``"corey-laptop"``.
        :param user_id: New user_id to record, e.g. ``"local"`` or
            ``"corey.zumar@databricks.com"``.
        :param configured_harnesses_json: JSON-encoded readiness map from
            the connecting host's hello, e.g.
            ``'{"claude-sdk": true}'``, or ``None`` when unreported.
            Written like the normal connect paths so a re-owned row
            carries fresh (not stale) readiness.
        :returns: The re-owned :class:`Host`, or ``None`` if no row holds
            *host_id* (caller falls through to a normal insert).
        """
        existing = session.execute(
            select(SqlHost).where(
                SqlHost.workspace_id == current_workspace_id(), SqlHost.host_id == host_id
            )
        ).scalar_one_or_none()
        if existing is None:
            return None
        created_at = existing.created_at
        now = now_epoch()
        session.execute(
            update(SqlHost)
            .where(
                SqlHost.workspace_id == current_workspace_id(),
                SqlHost.host_id == host_id,
            )
            .values(
                user_id=user_id,
                name=name,
                status=encode_host_status("online"),
                updated_at=now,
                configured_harnesses=configured_harnesses_json,
            )
        )
        if existing.user_id != user_id:
            # The previous owner's sharing decisions don't bind the new
            # one — drop the grants rather than let them silently carry
            # a teammate's access across an ownership change.
            session.execute(
                sql_delete(SqlHostPermission).where(
                    SqlHostPermission.workspace_id == current_workspace_id(),
                    SqlHostPermission.host_id == host_id,
                )
            )
        return Host(
            host_id=host_id,
            name=name,
            user_id=user_id,
            status="online",
            created_at=created_at,
            updated_at=now,
            sandbox_provider=existing.sandbox_provider,
            sandbox_id=existing.sandbox_id,
            configured_harnesses=_parse_configured_harnesses(configured_harnesses_json),
        )

    def set_offline(self, host_id: str) -> None:
        """
        Mark a host as offline when its WebSocket disconnects.

        No-op if the host does not exist (the disconnect callback
        may fire after a failed registration).

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(), SqlHost.host_id == host_id
                )
            ).scalar_one_or_none()
            if row is not None:
                row.status = encode_host_status("offline")
                row.updated_at = now_epoch()

    def update_harness_readiness(
        self,
        host_id: str,
        configured_harnesses: dict[str, HarnessAvailability],
    ) -> None:
        """Replace a connected host's live per-harness readiness map.

        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :param configured_harnesses: Current readiness keyed by harness spelling.
        """
        with self._session() as session:
            session.execute(
                update(SqlHost)
                .where(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.host_id == host_id,
                )
                .values(
                    configured_harnesses=json.dumps(configured_harnesses),
                    updated_at=now_epoch(),
                )
            )

    def heartbeat(self, host_id: str) -> None:
        """
        Refresh a host's last-seen timestamp while its tunnel is alive.

        Bumps ``updated_at`` to now so the liveness freshness gate
        (see :data:`HOST_LIVENESS_TTL_S`) keeps treating the host as
        online. Called from the host tunnel's ping loop every
        ``PING_INTERVAL_S``. Does not change ``status`` — a host whose
        ping loop is running is, by construction, still ``"online"``.

        No-op if the host does not exist.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        """
        # Single UPDATE rather than SELECT-then-mutate: this runs every
        # ping interval for every connected host, so the extra read is
        # pure overhead. A missing host simply matches no rows (a no-op).
        with self._session() as session:
            session.execute(
                update(SqlHost)
                .where(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.host_id == host_id,
                )
                .values(updated_at=now_epoch())
            )

    def is_online(self, host_id: str) -> bool:
        """
        Return whether a host is currently live, cross-replica.

        A host counts as live only when its row is ``status="online"``
        **and** its last-seen (``updated_at``) is within
        :data:`HOST_LIVENESS_TTL_S`. The freshness check is what
        catches a host that died without a graceful disconnect: the
        ``status`` flag alone stays ``"online"`` forever in that case
        (set_offline only runs on a clean tunnel close), so a stale
        timestamp is the only reliable signal that the host is gone.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: ``True`` when the host is online and its last-seen is
            fresh; ``False`` if unknown, offline, or stale.
        """
        row = self.get_host(host_id)
        return row is not None and host_is_live(row)

    def online_host_ids(self, host_ids: list[str]) -> set[str]:
        """
        Return the subset of ``host_ids`` that are currently live.

        Bulk variant of :meth:`is_online` for the sidebar online-dot
        batch path: one ``SELECT ... WHERE host_id IN (...)`` instead
        of a per-host query. Liveness applies the same
        status-plus-freshness gate as :meth:`is_online`, classifying
        every row against one consistent clock.

        :param host_ids: Host identifiers to check, e.g.
            ``["host_abc123", "host_def456"]``. Duplicates are
            tolerated; empty input returns an empty set without
            touching the database.
        :returns: The set of ids whose host row is online and fresh.
            Unknown, offline, or stale ids are absent.
        """
        if not host_ids:
            return set()
        unique_ids = list(set(host_ids))
        ref = now_epoch()
        with self._session() as session:
            rows = session.execute(
                select(SqlHost.host_id, SqlHost.status, SqlHost.updated_at).where(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.host_id.in_(unique_ids),
                )
            ).all()
        online_code = encode_host_status("online")
        return {
            row.host_id
            for row in rows
            if row.status == online_code and row.updated_at >= ref - HOST_LIVENESS_TTL_S
        }

    def list_hosts(self, user_id: str) -> list[Host]:
        """
        List all hosts owned by a specific user.

        Returns both online and offline hosts, ordered by
        ``updated_at`` descending (most recently active first).

        :param user_id: User ID to filter by, e.g.
            ``"corey.zumar@databricks.com"``.
        :returns: List of :class:`Host` entities.
        """
        with self._session() as session:
            rows = (
                session.query(SqlHost)
                .filter(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.user_id == user_id,
                )
                .order_by(SqlHost.updated_at.desc())
                .all()
            )
            return [_row_to_host(row) for row in rows]

    def list_hosts_accessible_by(self, user_id: str) -> list[Host]:
        """
        List hosts the user owns OR has been granted access to.

        Backs the host picker for shared team hosts: a host registered
        by a teammate appears here once they grant this user access.
        Ordered by ``updated_at`` descending, like :meth:`list_hosts`.

        The grant join is an ``EXISTS`` against the
        ``ix_host_permissions_user_id`` index, so this stays a single
        index-served query rather than reading the workspace's hosts
        and filtering in Python.

        :param user_id: The caller, e.g. ``"bob@example.com"``.
        :returns: List of :class:`Host` entities, owned and shared.
        """
        with self._session() as session:
            grant_exists = (
                select(SqlHostPermission.host_id)
                .where(
                    SqlHostPermission.workspace_id == current_workspace_id(),
                    SqlHostPermission.user_id == user_id,
                    SqlHostPermission.host_id == SqlHost.host_id,
                )
                .exists()
            )
            rows = (
                session.query(SqlHost)
                .filter(
                    SqlHost.workspace_id == current_workspace_id(),
                    or_(SqlHost.user_id == user_id, grant_exists),
                )
                .order_by(SqlHost.updated_at.desc())
                .all()
            )
            return [_row_to_host(row) for row in rows]

    def grant_host_access(self, host_id: str, user_id: str, level: str = HOST_LEVEL_USE) -> None:
        """
        Grant a user access to a host they do not own.

        Upserts, so re-sharing changes the level instead of appending a
        second grant, and two concurrent shares cannot lose one
        another's write. The caller is responsible for authorization —
        only the host's owner may share it.

        Granting to the host's own owner is a no-op: ownership already
        implies full access, and a redundant row would outlive an
        ownership change.

        :param host_id: The host to share, e.g. ``"host_a1b2c3d4..."``.
        :param user_id: The grantee, e.g. ``"bob@example.com"``.
        :param level: ``"read"`` or ``"use"`` (default). See
            :class:`HostGrant`.
        :raises ValueError: If *level* is not a known access level.
        """
        code = encode_host_permission_level(level)
        with self._session() as session:
            host = session.execute(
                select(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.host_id == host_id,
                )
            ).scalar_one_or_none()
            if host is None or host.user_id == user_id:
                return

            values = {
                "host_id": host_id,
                "user_id": user_id,
                "level": code,
                "created_at": now_epoch(),
            }
            index_elements = ["workspace_id", "host_id", "user_id"]
            dialect = self._engine.dialect.name
            if dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlHostPermission)
                    .values(**values)
                    .on_conflict_do_update(index_elements=index_elements, set_={"level": code})
                )
            elif dialect == "mysql":
                stmt = (
                    mysql_insert(SqlHostPermission)
                    .values(**values)
                    .on_duplicate_key_update(level=code)
                )
            else:
                stmt = (
                    pg_insert(SqlHostPermission)
                    .values(**values)
                    .on_conflict_do_update(index_elements=index_elements, set_={"level": code})
                )
            session.execute(stmt)

    def revoke_host_access(self, host_id: str, user_id: str) -> bool:
        """
        Revoke a user's granted access to a host.

        A plain delete — it can never strip the owner, whose access is
        ``hosts.user_id``, not a grant. No-op when no grant exists.

        :param host_id: The shared host, e.g. ``"host_a1b2c3d4..."``.
        :param user_id: The grantee to revoke, e.g. ``"bob@example.com"``.
        :returns: ``True`` if a grant was deleted, ``False`` if none existed.
        """
        with self._session() as session:
            result = session.execute(
                sql_delete(SqlHostPermission).where(
                    SqlHostPermission.workspace_id == current_workspace_id(),
                    SqlHostPermission.host_id == host_id,
                    SqlHostPermission.user_id == user_id,
                )
            )
            return result.rowcount > 0

    def get_host_access_level(self, host_id: str, user_id: str) -> str | None:
        """
        Return a user's granted access level on a host, if any.

        Grants only — an owner has no grant row, so callers must treat
        ownership separately (see
        :func:`omnigent.server.routes._host_launch.resolve_host_owner`).

        :param host_id: The host, e.g. ``"host_a1b2c3d4..."``.
        :param user_id: The user, e.g. ``"bob@example.com"``.
        :returns: ``"read"`` / ``"use"``, or ``None`` if not granted.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHostPermission).where(
                    SqlHostPermission.workspace_id == current_workspace_id(),
                    SqlHostPermission.host_id == host_id,
                    SqlHostPermission.user_id == user_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return decode_host_permission_level(row.level)

    def list_host_grants(self, host_id: str) -> list[HostGrant]:
        """
        List every grant on a host, oldest first.

        Powers the owner-facing "shared with" list. The owner is not
        included — ownership is not a grant.

        :param host_id: The host to query, e.g. ``"host_a1b2c3d4..."``.
        :returns: List of :class:`HostGrant` objects.
        """
        with self._session() as session:
            rows = (
                session.query(SqlHostPermission)
                .filter(
                    SqlHostPermission.workspace_id == current_workspace_id(),
                    SqlHostPermission.host_id == host_id,
                )
                .order_by(SqlHostPermission.created_at, SqlHostPermission.user_id)
                .all()
            )
            return [_row_to_grant(row) for row in rows]

    def get_host(self, host_id: str) -> Host | None:
        """
        Fetch a single host by ID.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: The :class:`Host` if found, otherwise ``None``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(), SqlHost.host_id == host_id
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _row_to_host(row)

    def register_managed_host(
        self,
        *,
        host_id: str,
        name: str,
        user_id: str,
        token: str,
        provider: str,
        sandbox_id: str,
        token_expires_at: int,
    ) -> Host:
        """
        Pre-register a server-managed sandbox host with its credential.

        Called by the managed-launch orchestration after the sandbox is
        provisioned and BEFORE the in-sandbox host process starts, so
        the launch token is resolvable by the time the host first dials
        the tunnel. The row is created ``"offline"``; the tunnel's
        normal ``upsert_on_connect`` flips it online when the host
        registers.

        If a row already exists for *host_id* (a RELAUNCH: the host
        identity is durable across sandbox generations so session
        bindings survive a dead sandbox), the credential and sandbox
        columns are overwritten in place — which atomically revokes the
        previous generation's token, since its digest no longer matches
        anything.

        :param host_id: Server-generated host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param name: Display name for the host picker, e.g.
            ``"managed-a1b2c3d4"``. Part of the table's
            ``(user_id, name)`` primary key.
        :param user_id: User the managed host acts for, e.g.
            ``"alice@example.com"``.
        :param token: The RAW launch token (hashed here, never stored),
            e.g. the value of ``secrets.token_urlsafe(32)``.
        :param provider: Sandbox provider name, e.g. ``"modal"``.
        :param sandbox_id: Provider-assigned sandbox id, e.g.
            ``"sb-a1b2c3"``.
        :param token_expires_at: Unix epoch seconds after which the
            token no longer authenticates.
        :returns: The registered :class:`Host`.
        :raises ValueError: If a row for *host_id* exists under a
            DIFFERENT user_id — a relaunch may only re-credential a host
            the same user owns.
        """
        now = now_epoch()
        token_hash = hash_host_launch_token(token)
        with self._session() as session:
            existing = session.execute(
                select(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(), SqlHost.host_id == host_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.user_id != user_id:
                    # Fail closed (W2-class boundary): re-crediting a host
                    # row hands its launch token holder the row owner's
                    # identity, so a cross-owner overwrite would be a host
                    # hijack. host_id is server-generated today (uuid4 per
                    # launch), so this can only fire on a bug or a forged
                    # id — refuse rather than re-own.
                    raise ValueError(
                        f"host {host_id!r} is registered to a different user; "
                        "refusing to re-credential it"
                    )
                existing.token_hash = token_hash
                existing.token_expires_at = token_expires_at
                existing.sandbox_provider = provider
                existing.sandbox_id = sandbox_id
                existing.updated_at = now
                return _row_to_host(existing)
            row = SqlHost(
                user_id=user_id,
                name=name,
                host_id=host_id,
                status=encode_host_status("offline"),
                created_at=now,
                updated_at=now,
                token_hash=token_hash,
                token_expires_at=token_expires_at,
                sandbox_provider=provider,
                sandbox_id=sandbox_id,
            )
            session.add(row)
            return _row_to_host(row)

    def resolve_launch_token(self, host_id: str, token: str) -> Host | None:
        """
        Resolve a launch token presented for *host_id* to its managed host.

        The host tunnel's auth path for managed hosts, whose endpoint is
        ``/hosts/{host_id}/tunnel`` — so the connecting peer names the
        host it claims to be, and the token proves the claim. The row is
        fetched by its ``(workspace_id, host_id)`` primary key and the
        stored SHA-256 digest is compared to the presented token's digest
        with :func:`hmac.compare_digest`, so the equality is constant-time
        and leaks no timing oracle on the raw token. Presenting a token
        for the wrong ``host_id`` fails closed: the named row's digest
        won't match. Expired tokens do not authenticate.

        :param host_id: The host the peer claims to be, from the tunnel
            path, e.g. ``"host_a1b2c3d4..."``.
        :param token: The raw token presented by the connecting host.
        :returns: The matching :class:`Host` whose token is unexpired,
            or ``None`` when the host is unknown, the token does not match,
            or the token is expired.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.host_id == host_id,
                )
            ).scalar_one_or_none()
            # token_expires_at is written together with token_hash, so a
            # credentialled row always carries both; a row with either
            # cleared (external host, or a revoked credential) never
            # authenticates.
            if row is None or row.token_hash is None or row.token_expires_at is None:
                return None
            if not hmac.compare_digest(row.token_hash, hash_host_launch_token(token)):
                return None
            if row.token_expires_at < now_epoch():
                return None
            return _row_to_host(row)

    def delete_host(self, host_id: str) -> None:
        """
        Delete a host row entirely.

        Managed-host teardown: removes the host from the picker AND
        revokes its launch token in one operation (the row IS the
        credential). Explicitly nulls ``conversations.host_id`` for any
        sessions still bound to this host — the DB no longer cascades
        this via FK. No-op when the row does not exist — deletion is
        invoked from best-effort cleanup paths that may race.

        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        """
        with self._session() as session:
            session.execute(
                update(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.host_id == host_id,
                )
                .values(host_id=None)
            )
            session.execute(
                sql_delete(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(),
                    SqlHost.host_id == host_id,
                )
            )
            # Grants are not FK-cascaded (Rule R032), so drop them here.
            # A stale grant would otherwise silently re-authorize a
            # teammate if this host_id were ever reused.
            session.execute(
                sql_delete(SqlHostPermission).where(
                    SqlHostPermission.workspace_id == current_workspace_id(),
                    SqlHostPermission.host_id == host_id,
                )
            )

    def revoke_launch_token(self, host_id: str) -> None:
        """
        Clear a managed host's launch credential, keeping the row.

        Relaunch-failure cleanup: a failed sandbox RELAUNCH must revoke
        the token it armed (the new sandbox never came up to use it)
        without deleting the durable host row — the session binding
        survives, and the next relaunch attempt re-arms a fresh token
        via :meth:`register_managed_host`. Contrast :meth:`delete_host`,
        which is full teardown. No-op when the row does not exist.

        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(
                    SqlHost.workspace_id == current_workspace_id(), SqlHost.host_id == host_id
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.token_hash = None
            row.token_expires_at = None
            row.updated_at = now_epoch()
