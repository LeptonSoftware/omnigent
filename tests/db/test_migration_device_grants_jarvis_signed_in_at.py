"""Rows that predate the Jarvis sign-in column never read as Jarvis sign-ins."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.server.device_grant_store import DeviceGrantStore


def test_existing_jarvis_named_grant_upgrades_without_a_sign_in(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'jarvis-column.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "mm1a2b3c4d5e")
        connection.execute(
            sa.text(
                "INSERT INTO device_grants (id, device_code_hash, user_code, status, client_id, "
                "user_id, created_at, expires_at, approved_at, refresh_token_hash) VALUES "
                "('old', 'device', 'code', 4, 'jarvis', 'alice', 1, 2, 1, 'refresh')"
            )
        )
        command.upgrade(config, "head")
        assert (
            connection.execute(
                sa.text("SELECT jarvis_signed_in_at FROM device_grants WHERE id = 'old'")
            ).scalar_one()
            is None
        )
    grant = DeviceGrantStore(uri).get_by_refresh_hash("refresh")
    assert grant is not None
    assert (grant.client_id, grant.jarvis_signed_in_at) == ("jarvis", None)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "mm1a2b3c4d5e")
        columns = {
            column["name"] for column in sa.inspect(connection).get_columns("device_grants")
        }
        assert "jarvis_signed_in_at" not in columns
        command.upgrade(config, "head")
