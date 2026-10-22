"""Session tokens signed with Ed25519, verified offline from the JWKS.

Covers the signer (mint/verify, ``kid`` lookup, ``iss``/``aud``/``exp``,
the HS256 legacy window), its env settings and start-up refusal, and the
wiring: ``UnifiedAuthProvider`` accepting the tokens and
``GET /.well-known/jwks.json`` on a real ``create_app``.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider, create_auth_provider
from omnigent.server.oidc import mint_session_cookie, mint_session_token
from omnigent.server.session_signing import (
    JWKS_CACHE_CONTROL,
    MODE_EDDSA,
    PublicSigningKey,
    SessionSigningConfigError,
    SessionSigningKeys,
    UnknownSigningKeyError,
    jwk_thumbprint,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_SECRET = bytes.fromhex("ab" * 32)
_ISSUER = "https://omni.example"
_SIGNING_ENV = (
    "OMNIGENT_SESSION_SIGNING",
    "OMNIGENT_SESSION_SIGNING_KEY",
    "OMNIGENT_SESSION_SIGNING_KEY_FILE",
    "OMNIGENT_SESSION_SIGNING_PREVIOUS_KEY",
    "OMNIGENT_SESSION_SIGNING_PREVIOUS_KEY_FILE",
    "OMNIGENT_SESSION_TOKEN_ISSUER",
    "OMNIGENT_SESSION_TOKEN_AUDIENCE",
    "OMNIGENT_LEGACY_HS256_UNTIL",
)


@pytest.fixture(autouse=True)
def _clean_signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SIGNING_ENV:
        monkeypatch.delenv(name, raising=False)


def _eddsa_keys(
    signing_key: Ed25519PrivateKey | None = None,
    *,
    previous: Ed25519PrivateKey | None = None,
    legacy_until: datetime | None = None,
    issuer: str = _ISSUER,
    audience: tuple[str, ...] = ("omnigent", "jarvis"),
) -> SessionSigningKeys:
    return SessionSigningKeys(
        legacy_secret=_SECRET,
        mode=MODE_EDDSA,
        signing_key=signing_key or Ed25519PrivateKey.generate(),
        previous_key=PublicSigningKey.of(previous.public_key()) if previous else None,
        issuer=issuer,
        audience=audience,
        legacy_hs256_until=legacy_until,
    )


def _private_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _public_pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _public_from_jwk(jwk: dict[str, str]) -> Ed25519PublicKey:
    raw = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    return Ed25519PublicKey.from_public_bytes(raw)


def _hs256(claims: dict[str, object]) -> str:
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    return {"sub": "alice@example.com", "iat": now, "exp": now + 600, "provider": "oidc"} | (
        overrides
    )


# ── Signer ───────────────────────────────────────────────────────


def test_mint_then_verify_round_trips_with_contract_header_and_claims() -> None:
    keys = _eddsa_keys()
    token = mint_session_token("alice@example.com", keys, 600, "oidc")

    header = jwt.get_unverified_header(token)
    assert header == {"alg": "EdDSA", "kid": keys.current_key.kid}
    claims = keys.decode(token)
    assert claims["sub"] == "alice@example.com"
    assert claims["iss"] == _ISSUER
    assert claims["aud"] == ["omnigent", "jarvis"]
    assert claims["provider"] == "oidc"
    assert claims["exp"] - claims["iat"] == 600


def test_token_verifies_with_only_the_published_jwk() -> None:
    """What Jarvis does: verify with the JWKS entry, no Omnigent code."""
    keys = _eddsa_keys()
    token = mint_session_cookie("alice@example.com", keys, 8, "oidc")

    (jwk,) = keys.jwks()["keys"]
    claims = jwt.decode(
        token,
        _public_from_jwk(jwk),
        algorithms=["EdDSA"],
        audience="jarvis",
        issuer=_ISSUER,
    )
    assert claims["sub"] == "alice@example.com"


def test_jwks_lists_current_then_previous_key() -> None:
    current, previous = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    jwks = _eddsa_keys(current, previous=previous).jwks()

    assert [set(k) for k in jwks["keys"]] == [{"kty", "crv", "x", "kid", "use", "alg"}] * 2
    assert all(
        (k["kty"], k["crv"], k["use"], k["alg"]) == ("OKP", "Ed25519", "sig", "EdDSA")
        for k in jwks["keys"]
    )
    assert [k["kid"] for k in jwks["keys"]] == [
        jwk_thumbprint(current.public_key()),
        jwk_thumbprint(previous.public_key()),
    ]


def test_kid_is_the_rfc7638_thumbprint() -> None:
    """RFC 8037 appendix A.3: the Ed25519 example key and its thumbprint."""
    key = Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo=")
    )
    assert jwk_thumbprint(key) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"


def test_kid_selects_the_previous_key_after_rotation() -> None:
    old = Ed25519PrivateKey.generate()
    token = _eddsa_keys(old).encode(_claims())

    rotated = _eddsa_keys(Ed25519PrivateKey.generate(), previous=old)
    assert rotated.decode(token)["sub"] == "alice@example.com"
    with pytest.raises(jwt.InvalidTokenError):
        _eddsa_keys(Ed25519PrivateKey.generate()).decode(token)


def test_unknown_kid_is_refused_even_when_the_signature_is_valid() -> None:
    key = Ed25519PrivateKey.generate()
    keys = _eddsa_keys(key)
    forged = jwt.encode(
        _claims(iss=_ISSUER, aud=["omnigent"]), key, algorithm="EdDSA", headers={"kid": "other"}
    )
    with pytest.raises(UnknownSigningKeyError):
        keys.decode(forged)


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": ["jarvis"]},
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
    ],
    ids=["aud-without-omnigent", "aud-foreign", "iss-foreign"],
)
def test_wrong_audience_or_issuer_is_refused(overrides: dict[str, object]) -> None:
    key = Ed25519PrivateKey.generate()
    keys = _eddsa_keys(key)
    token = jwt.encode(
        _claims(**({"iss": _ISSUER, "aud": ["omnigent", "jarvis"]} | overrides)),
        key,
        algorithm="EdDSA",
        headers={"kid": keys.current_key.kid},
    )
    with pytest.raises(jwt.InvalidTokenError):
        keys.decode(token)


def test_missing_issuer_is_refused() -> None:
    key = Ed25519PrivateKey.generate()
    keys = _eddsa_keys(key)
    token = jwt.encode(
        _claims(aud=["omnigent"]), key, algorithm="EdDSA", headers={"kid": keys.current_key.kid}
    )
    with pytest.raises(jwt.MissingRequiredClaimError):
        keys.decode(token)


def test_expired_token_is_refused() -> None:
    keys = _eddsa_keys()
    past = int(time.time()) - 3600
    token = keys.encode(_claims(iat=past, exp=past + 60))
    with pytest.raises(jwt.ExpiredSignatureError):
        keys.decode(token)


def test_hs256_is_accepted_before_the_legacy_date_and_refused_after() -> None:
    token = _hs256(_claims())
    now = datetime.now(timezone.utc)

    assert _eddsa_keys(legacy_until=now + timedelta(days=1)).decode(token)["sub"] == (
        "alice@example.com"
    )
    with pytest.raises(jwt.InvalidAlgorithmError):
        _eddsa_keys(legacy_until=now - timedelta(seconds=1)).decode(token)
    with pytest.raises(jwt.InvalidAlgorithmError):
        _eddsa_keys(legacy_until=None).decode(token)


def test_hs256_mode_mints_and_accepts_the_pre_eddsa_token() -> None:
    keys = SessionSigningKeys.hs256(_SECRET)
    token = mint_session_token("alice@example.com", keys, 600, "accounts")

    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    assert jwt.decode(token, _SECRET, algorithms=["HS256"])["sub"] == "alice@example.com"
    assert keys.decode(token)["sub"] == "alice@example.com"
    assert keys.jwks() == {"keys": []}


def test_none_and_foreign_algorithms_are_refused() -> None:
    keys = _eddsa_keys()
    unsigned = jwt.encode(_claims(), None, algorithm="none")
    hs512 = jwt.encode(_claims(), _SECRET * 2, algorithm="HS512")
    for token in (unsigned, hs512):
        with pytest.raises(jwt.InvalidAlgorithmError):
            keys.decode(token)


def test_verification_cache_never_outlives_the_legacy_deadline() -> None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    keys = _eddsa_keys(legacy_until=deadline)
    token = _hs256(_claims(exp=int(time.time()) + 3600))

    assert keys.accepted_until(token, keys.decode(token)) == deadline.timestamp()


# ── Provider wiring ──────────────────────────────────────────────


class _Request:
    def __init__(self, token: str) -> None:
        self.cookies: dict[str, str] = {}
        self.headers = {"Authorization": f"Bearer {token}"}
        self.scope: dict[str, object] = {}


def _accounts_config(keys: SessionSigningKeys | None) -> AccountsConfig:
    return AccountsConfig(
        cookie_secret=_SECRET,
        session_ttl_hours=8,
        base_url=_ISSUER,
        init_admin_password=None,
        invite_ttl_seconds=3600,
        magic_ttl_seconds=300,
        session_keys=keys,
    )


def test_provider_accepts_eddsa_bearer_and_mints_eddsa_runner_tokens() -> None:
    keys = _eddsa_keys(legacy_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    provider = UnifiedAuthProvider(source="accounts", accounts_config=_accounts_config(keys))

    runner_token = provider.mint_runner_token("alice", 300)
    assert runner_token is not None
    assert jwt.get_unverified_header(runner_token)["alg"] == "EdDSA"
    assert provider.get_user_id(_Request(runner_token)) == "alice"
    assert provider.get_user_id(_Request(_hs256(_claims(sub="alice")))) is None
    stranger = _eddsa_keys().encode(_claims(sub="alice"))
    assert provider.get_user_id(_Request(stranger)) is None


# ── Settings and start-up refusal ────────────────────────────────


def _accounts_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", _SECRET.hex())
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", _ISSUER)


def test_startup_refuses_eddsa_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _accounts_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_SESSION_SIGNING", "eddsa")

    with pytest.raises(SessionSigningConfigError, match="OMNIGENT_SESSION_SIGNING_KEY_FILE"):
        create_auth_provider()


def test_startup_loads_key_file_and_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current, previous = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    (tmp_path / "current.pem").write_bytes(_private_pem(current))
    _accounts_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_SESSION_SIGNING_KEY_FILE", str(tmp_path / "current.pem"))
    monkeypatch.setenv(
        "OMNIGENT_SESSION_SIGNING_PREVIOUS_KEY",
        _public_pem(previous).decode().replace("\n", "\\n"),
    )
    monkeypatch.setenv("OMNIGENT_SESSION_TOKEN_AUDIENCE", "omnigent, jarvis, console")
    monkeypatch.setenv("OMNIGENT_LEGACY_HS256_UNTIL", "2099-01-31")

    provider = create_auth_provider()
    assert isinstance(provider, UnifiedAuthProvider)
    keys = provider._accounts_config.signing_keys

    assert keys.mode == MODE_EDDSA
    assert keys.issuer == _ISSUER
    assert keys.audience == ("omnigent", "jarvis", "console")
    assert keys.legacy_hs256_until == datetime(2099, 1, 31, tzinfo=timezone.utc)
    assert [k.kid for k in keys.verification_keys] == [
        jwk_thumbprint(current.public_key()),
        jwk_thumbprint(previous.public_key()),
    ]


def test_without_key_settings_the_server_keeps_hs256(monkeypatch: pytest.MonkeyPatch) -> None:
    _accounts_env(monkeypatch)
    provider = create_auth_provider()
    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._accounts_config.signing_keys.mode == "hs256"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"OMNIGENT_SESSION_SIGNING": "rs256"}, "must be 'eddsa' or 'hs256'"),
        ({"OMNIGENT_SESSION_SIGNING_KEY_FILE": "/nonexistent/key.pem"}, "is not readable"),
        ({"OMNIGENT_SESSION_SIGNING_KEY": "not a pem"}, "not an unencrypted PEM"),
        ({"OMNIGENT_SESSION_TOKEN_AUDIENCE": "jarvis"}, "must include 'omnigent'"),
        ({"OMNIGENT_LEGACY_HS256_UNTIL": "next week"}, "YYYY-MM-DD"),
        ({"OMNIGENT_LEGACY_HS256_UNTIL": "2099-01-01T00:00:00"}, "UTC offset"),
    ],
)
def test_invalid_settings_refuse_to_start(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    _accounts_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SessionSigningConfigError, match=message):
        create_auth_provider()


def test_refusal_never_echoes_key_material(monkeypatch: pytest.MonkeyPatch) -> None:
    rsa_like = "-----BEGIN PRIVATE KEY-----\\nc2VjcmV0LWtleS1ieXRlcw==\\n-----END PRIVATE KEY-----"
    _accounts_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_SESSION_SIGNING_KEY", rsa_like)
    with pytest.raises(SessionSigningConfigError) as raised:
        create_auth_provider()
    assert "c2VjcmV0" not in str(raised.value)


# ── JWKS route ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jwks_route_is_public_and_cacheable(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    keys = _eddsa_keys()
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(
            source="accounts", accounts_config=_accounts_config(keys)
        ),
        account_store=SqlAlchemyAccountStore(db_uri),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/.well-known/jwks.json")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == JWKS_CACHE_CONTROL
    assert resp.json() == keys.jwks()
