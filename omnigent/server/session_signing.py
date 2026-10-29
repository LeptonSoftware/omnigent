"""Session token signing: Ed25519 (EdDSA) with a published JWKS.

fork: every token the server issues for a signed-in identity
(browser cookie, CLI login, device/login-grant access token, machine
client-credential token, managed-runner token) is signed by
:class:`SessionSigningKeys`. With an Ed25519 key configured the token is
an EdDSA JWT carrying ``iss`` and ``aud``, and its public key is served at
``GET /.well-known/jwks.json`` (see ``routes/jwks.py``), so any service
(Jarvis first) can verify it offline without a secret that could also mint.

Settings (environment, like the other auth secrets; never the YAML file):

- ``OMNIGENT_SESSION_SIGNING`` — ``eddsa`` or ``hs256``. Unset means
  ``eddsa`` when a key is configured, else ``hs256``. ``eddsa`` without a
  key refuses to start (:class:`SessionSigningConfigError`).
- ``OMNIGENT_SESSION_SIGNING_KEY`` / ``OMNIGENT_SESSION_SIGNING_KEY_FILE`` —
  the Ed25519 private key as PKCS#8 PEM, inline or as a file path (set one).
- ``OMNIGENT_SESSION_SIGNING_PREVIOUS_KEY`` /
  ``OMNIGENT_SESSION_SIGNING_PREVIOUS_KEY_FILE`` — the previous key's
  PUBLIC PEM, kept in the JWKS and accepted after a rotation.
- ``OMNIGENT_SESSION_TOKEN_ISSUER`` — ``iss``; defaults to the server's
  public base URL (OIDC redirect URI origin, or the accounts base URL).
- ``OMNIGENT_SESSION_TOKEN_AUDIENCE`` — comma list for the ``aud`` of
  identity tokens; default ``omnigent,jarvis``. Must contain ``omnigent``:
  this server verifies its own tokens against that audience.
- ``OMNIGENT_LEGACY_HS256_UNTIL`` — ``YYYY-MM-DD`` (00:00 UTC) or an ISO
  datetime with offset. While ``eddsa`` is on, HS256 tokens signed with the
  cookie secret are accepted strictly before this instant and refused from
  it on. Unset means refused at once. Set it to at least the deploy date
  plus the longest token lifetime the old server minted (cookies and CLI
  logins live ``OMNIGENT_OIDC_SESSION_TTL_HOURS`` /
  ``OMNIGENT_ACCOUNTS_SESSION_TTL_HOURS``, 30 days on prod) plus a margin,
  so no HS256 token outlives the window it was minted under.
  Clients that still hold one when the window closes recover by
  themselves: a 401 makes them refresh once through their refresh grant.

Production must set ``OMNIGENT_SESSION_SIGNING=eddsa`` explicitly. Docker
compose passes ``""`` for an unset variable, and an empty mode with no key
configured (say the key variable was left out of the env file) falls back
to ``hs256`` with only a log warning; an explicit ``eddsa`` refuses to
start instead.

Two token kinds (:data:`TokenKind`), chosen explicitly at every mint site:
``identity`` tokens (browser cookie, CLI and host login, the login grant's
refresh) carry the configured audience, ``jarvis`` included; ``internal``
tokens (managed-runner tokens handed to sandboxes, machine client
credentials, third-party device grants) carry ``aud: ["omnigent"]`` only,
so no other service accepts them.

``hs256`` mode mints exactly what the server minted before this change and
always accepts HS256, whatever ``OMNIGENT_LEGACY_HS256_UNTIL`` says: it is
the rollback switch, and a rollback that also expired every HS256 token
would log everyone out. It still verifies EdDSA tokens and publishes the
JWKS when a key is configured, so flipping ``eddsa`` back to ``hs256``
rolls back without logging out anyone who got an EdDSA token. The HS256
path, this mode, and the ``bytes`` form of :func:`as_signing_keys` go
away in the follow-up that removes legacy tokens.

Verification, on both algorithms: ``exp``, ``iat`` and ``sub`` are
required, ``exp``/``iat``/``nbf`` must be JSON numbers, ``iat``/``nbf``
may lie up to :data:`CLOCK_SKEW_LEEWAY_SECONDS` in the future (``exp`` is
exact), and the signature segment
must be canonical unpadded base64url (PyJWT alone accepts a padded or
bit-flipped final character, so one token would have several spellings).
EdDSA tokens additionally need ``iss`` and ``aud``; HS256 tokens never had
them.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

if TYPE_CHECKING:
    from omnigent.server.accounts_config import AccountsConfig
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.oidc import OIDCConfig

_ConfigT = TypeVar("_ConfigT", "OIDCConfig", "AccountsConfig")

_logger = logging.getLogger(__name__)

MODE_ENV = "OMNIGENT_SESSION_SIGNING"
KEY_ENV = "OMNIGENT_SESSION_SIGNING_KEY"
KEY_FILE_ENV = "OMNIGENT_SESSION_SIGNING_KEY_FILE"
PREVIOUS_KEY_ENV = "OMNIGENT_SESSION_SIGNING_PREVIOUS_KEY"
PREVIOUS_KEY_FILE_ENV = "OMNIGENT_SESSION_SIGNING_PREVIOUS_KEY_FILE"
ISSUER_ENV = "OMNIGENT_SESSION_TOKEN_ISSUER"
AUDIENCE_ENV = "OMNIGENT_SESSION_TOKEN_AUDIENCE"
LEGACY_UNTIL_ENV = "OMNIGENT_LEGACY_HS256_UNTIL"

MODE_EDDSA = "eddsa"
MODE_HS256 = "hs256"
SERVER_AUDIENCE = "omnigent"
DEFAULT_AUDIENCE: tuple[str, ...] = ("omnigent", "jarvis")
JWKS_CACHE_CONTROL = "public, max-age=300"
# Same tolerance #66 gives the IdP's id_token: with none, a verifier one
# second behind the minting clock refuses a fresh token ("iat" in the future).
# It applies to iat and nbf only; exp stays exact (see SessionSigningKeys.decode).
CLOCK_SKEW_LEEWAY_SECONDS = 60

TokenKind = Literal["identity", "internal"]
"""Who a token is for. ``identity``: a signed-in person (cookie, CLI and host
login and its refresh); carries the configured audience, ``jarvis`` included.
``internal``: runner, sandbox, machine and third-party device tokens;
``aud`` is ``["omnigent"]`` only."""
IDENTITY: TokenKind = "identity"
INTERNAL: TokenKind = "internal"

_REQUIRED_CLAIMS = ("exp", "iat", "sub")
_NUMERIC_CLAIMS = ("exp", "iat", "nbf")
_B64URL_UNPADDED = re.compile(r"[A-Za-z0-9_-]+")


class SessionSigningConfigError(RuntimeError):
    """The session-signing settings cannot produce a working signer.

    Raised at startup (from :func:`omnigent.server.auth.create_auth_provider`)
    so a misconfigured server refuses to boot instead of minting tokens no
    verifier accepts. Messages name the setting and never echo key material.
    """


class UnknownSigningKeyError(jwt.InvalidTokenError):
    """An EdDSA token whose ``kid`` names no current or previous key."""


@dataclass(frozen=True)
class PublicSigningKey:
    """One Ed25519 verification key and its RFC 7638 thumbprint ``kid``."""

    kid: str
    key: Ed25519PublicKey

    @staticmethod
    def of(key: Ed25519PublicKey) -> PublicSigningKey:
        return PublicSigningKey(kid=jwk_thumbprint(key), key=key)

    def jwk(self) -> dict[str, str]:
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": _okp_x(self.key),
            "kid": self.kid,
            "use": "sig",
            "alg": "EdDSA",
        }


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _okp_x(key: Ed25519PublicKey) -> str:
    return _b64url(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def jwk_thumbprint(key: Ed25519PublicKey) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url) of an Ed25519 public key.

    The required members for an OKP key are ``crv``, ``kty`` and ``x``, in
    lexicographic order with no whitespace.
    """
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": _okp_x(key)}, separators=(",", ":"), sort_keys=True
    )
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


@dataclass(frozen=True)
class SessionSigningKeys:
    """Mint and verify session tokens.

    Build with :meth:`from_env` (the server) or :meth:`hs256` (a bare
    cookie secret: tests, dev, and every config that has no key yet).

    :param legacy_secret: The cookie secret; HMAC key of HS256 tokens.
    :param mode: :data:`MODE_EDDSA` or :data:`MODE_HS256` — what is minted.
    :param signing_key: Ed25519 private key; required in EdDSA mode.
    :param previous_key: Retired key still published and accepted.
    :param issuer: ``iss`` minted and required on EdDSA tokens.
    :param audience: ``aud`` minted on EdDSA tokens.
    :param legacy_hs256_until: In EdDSA mode, HS256 is accepted strictly
        before this instant. ``None`` refuses HS256 at once.
    """

    legacy_secret: bytes
    mode: str = MODE_HS256
    signing_key: Ed25519PrivateKey | None = None
    previous_key: PublicSigningKey | None = None
    issuer: str | None = None
    audience: tuple[str, ...] = DEFAULT_AUDIENCE
    legacy_hs256_until: datetime | None = None

    @staticmethod
    def hs256(cookie_secret: bytes) -> SessionSigningKeys:
        """Keys that mint and accept HS256 only, exactly as before VIN-3238."""
        return SessionSigningKeys(legacy_secret=cookie_secret)

    @property
    def current_key(self) -> PublicSigningKey | None:
        if self.signing_key is None:
            return None
        return PublicSigningKey.of(self.signing_key.public_key())

    @property
    def verification_keys(self) -> tuple[PublicSigningKey, ...]:
        return tuple(k for k in (self.current_key, self.previous_key) if k is not None)

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """The JWKS document: current key first, then the previous one."""
        return {"keys": [k.jwk() for k in self.verification_keys]}

    def accepts_hs256(self, now: float | None = None) -> bool:
        if self.mode == MODE_HS256:
            return True
        if self.legacy_hs256_until is None:
            return False
        return (time.time() if now is None else now) < self.legacy_hs256_until.timestamp()

    def audience_for(self, kind: TokenKind) -> tuple[str, ...]:
        """The ``aud`` an EdDSA token of *kind* carries."""
        if kind == IDENTITY:
            return self.audience
        if kind == INTERNAL:
            return (SERVER_AUDIENCE,)
        raise ValueError(f"unknown token kind {kind!r}")

    def encode(self, claims: dict[str, Any], *, kind: TokenKind) -> str:
        """Sign *claims*. EdDSA mode adds ``iss``, the *kind*'s ``aud`` and a ``kid`` header.

        :param kind: :data:`IDENTITY` or :data:`INTERNAL`; no default, so
            every mint site states who may accept the token.
        """
        audience = self.audience_for(kind)
        if self.mode == MODE_HS256:
            return jwt.encode(claims, self.legacy_secret, algorithm="HS256")
        assert self.signing_key is not None and self.issuer is not None
        current = self.current_key
        assert current is not None
        payload = {**claims, "iss": self.issuer, "aud": list(audience)}
        return jwt.encode(
            payload,
            self.signing_key,
            algorithm="EdDSA",
            headers={"kid": current.kid, "typ": None},
        )

    def decode(self, token: str) -> dict[str, Any]:
        """Verify *token* and return its claims.

        Both algorithms: the signature segment must be canonical
        (:func:`_require_canonical_signature`), ``exp``, ``iat`` and ``sub``
        must be present, time claims must be numbers
        (:func:`_require_numeric_times`), and ``iat``/``nbf`` may lie up to
        :data:`CLOCK_SKEW_LEEWAY_SECONDS` in the future. ``exp`` gets no
        leeway: a verifier ahead of the minter refusing a token a minute
        early only makes the client refresh, while a grace period would keep
        every token alive past the lifetime it was minted with.
        EdDSA: the ``kid`` must name a published key, and ``iss`` and ``aud``
        (must include ``omnigent``) are checked too.
        HS256: accepted only while :meth:`accepts_hs256`; no ``iss``/``aud``,
        which tokens minted before EdDSA never carried. Any other ``alg`` is
        refused.

        :raises jwt.InvalidTokenError: On any failure.
        """
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg in ("EdDSA", "HS256"):
            _require_canonical_signature(token)
        if alg == "EdDSA":
            kid = header.get("kid")
            key = next((k.key for k in self.verification_keys if k.kid == kid), None)
            if key is None:
                raise UnknownSigningKeyError(f"no published key has kid {kid!r}")
            claims = jwt.decode(
                token,
                key,
                algorithms=["EdDSA"],
                audience=SERVER_AUDIENCE,
                issuer=self.issuer,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": [*_REQUIRED_CLAIMS, "iss", "aud"], "verify_exp": False},
            )
        elif alg == "HS256":
            if not self.accepts_hs256():
                raise jwt.InvalidAlgorithmError("HS256 session tokens are no longer accepted")
            claims = jwt.decode(
                token,
                self.legacy_secret,
                algorithms=["HS256"],
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": list(_REQUIRED_CLAIMS), "verify_exp": False},
            )
        else:
            raise jwt.InvalidAlgorithmError(f"unsupported session token alg {alg!r}")
        _require_numeric_times(claims)
        if claims["exp"] <= time.time():
            raise jwt.ExpiredSignatureError("Signature has expired")
        return claims

    def accepted_until(self, token: str, claims: dict[str, Any]) -> float:
        """Epoch second until which an already-verified token stays valid.

        ``exp``, capped at the legacy deadline for an HS256 token in EdDSA
        mode, so a verification cache never outlives the cut-off.
        """
        exp = float(claims.get("exp", 0))
        if self.mode == MODE_EDDSA and jwt.get_unverified_header(token).get("alg") == "HS256":
            deadline = self.legacy_hs256_until.timestamp() if self.legacy_hs256_until else 0.0
            return min(exp, deadline)
        return exp

    @staticmethod
    def from_env(cookie_secret: bytes, *, default_issuer: str) -> SessionSigningKeys:
        """Read the ``OMNIGENT_SESSION_*`` settings (see the module docstring).

        :param cookie_secret: The provider's cookie secret (HS256 key).
        :param default_issuer: The server's public base URL, used as ``iss``
            unless ``OMNIGENT_SESSION_TOKEN_ISSUER`` overrides it.
        :raises SessionSigningConfigError: On any invalid or missing setting.
        """
        signing_pem = _read_pem(KEY_ENV, KEY_FILE_ENV)
        previous_pem = _read_pem(PREVIOUS_KEY_ENV, PREVIOUS_KEY_FILE_ENV)

        raw_mode = os.environ.get(MODE_ENV, "").strip().lower()
        mode = raw_mode or (MODE_EDDSA if signing_pem is not None else MODE_HS256)
        if mode not in (MODE_EDDSA, MODE_HS256):
            raise SessionSigningConfigError(
                f"{MODE_ENV} must be {MODE_EDDSA!r} or {MODE_HS256!r}, got {raw_mode!r}"
            )
        if mode == MODE_EDDSA and signing_pem is None:
            raise SessionSigningConfigError(
                f"{MODE_ENV}={MODE_EDDSA} but no Ed25519 private key is configured: set "
                f"{KEY_FILE_ENV} to a PKCS#8 PEM file (or {KEY_ENV} to the PEM itself). "
                f"Generate one with `openssl genpkey -algorithm ed25519`. "
                f"Refusing to start rather than mint tokens nothing can verify."
            )

        signing_key = _load_private(signing_pem) if signing_pem is not None else None
        previous_key = _load_public(previous_pem) if previous_pem is not None else None
        if signing_key is not None and previous_key is not None:
            if previous_key.kid == jwk_thumbprint(signing_key.public_key()):
                raise SessionSigningConfigError(
                    f"{PREVIOUS_KEY_ENV} is the current key; set it to the retired key or unset it"
                )

        issuer = (os.environ.get(ISSUER_ENV, "").strip() or default_issuer).rstrip("/")
        if signing_key is not None or previous_key is not None:
            parts = urlsplit(issuer)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise SessionSigningConfigError(
                    f"session token issuer must be an absolute http(s) URL; got {issuer!r} "
                    f"(set {ISSUER_ENV})"
                )

        raw_audience = os.environ.get(AUDIENCE_ENV, "").strip()
        audience = (
            tuple(a.strip() for a in raw_audience.split(",") if a.strip())
            if raw_audience
            else DEFAULT_AUDIENCE
        )
        if SERVER_AUDIENCE not in audience:
            raise SessionSigningConfigError(
                f"{AUDIENCE_ENV} must include {SERVER_AUDIENCE!r} (this server verifies its own "
                f"tokens against it); got {raw_audience!r}"
            )

        keys = SessionSigningKeys(
            legacy_secret=cookie_secret,
            mode=mode,
            signing_key=signing_key,
            previous_key=previous_key,
            issuer=issuer or None,
            audience=audience,
            legacy_hs256_until=_parse_legacy_until(os.environ.get(LEGACY_UNTIL_ENV, "")),
        )
        current = keys.current_key
        _logger.info(
            "session signing: mode=%s kid=%s previous_kid=%s iss=%s aud=%s hs256_until=%s",
            mode,
            current.kid if current else None,
            previous_key.kid if previous_key else None,
            keys.issuer,
            ",".join(audience),
            keys.legacy_hs256_until.isoformat() if keys.legacy_hs256_until else None,
        )
        if mode == MODE_HS256:
            _logger.warning(
                "session signing: minting HS256 session tokens (%s unset or hs256); other "
                "services cannot verify them. Set an Ed25519 key to enable EdDSA + JWKS.",
                MODE_ENV,
            )
        return keys


def _require_canonical_signature(token: str) -> None:
    """Refuse a token whose signature segment is not canonical base64url.

    PyJWT decodes the signature leniently: ``=`` padding is accepted, and a
    changed final character that only flips the unused low bits decodes to
    the same bytes. Each of those spellings would verify, so one token would
    have several valid strings (and several verification-cache entries).
    Only the exact unpadded encoding of the decoded bytes is accepted.

    :raises jwt.DecodeError: On any non-canonical signature segment.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise jwt.DecodeError("a session token has exactly three segments")
    signature = parts[2]
    if not _B64URL_UNPADDED.fullmatch(signature):
        raise jwt.DecodeError("signature segment is not unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (binascii.Error, ValueError):
        raise jwt.DecodeError("signature segment is not base64url") from None
    if _b64url(raw) != signature:
        raise jwt.DecodeError("signature segment is not canonical base64url")


def _require_numeric_times(claims: dict[str, Any]) -> None:
    """Refuse time claims that are not finite JSON numbers.

    PyJWT coerces ``"exp": "1893456000"`` with ``int()`` and accepts a
    boolean ``iat``. A verifier that reads the raw claim (Jarvis, or the
    cache in :meth:`SessionSigningKeys.accepted_until`) would disagree with
    it, so only ints and floats pass.

    :raises jwt.InvalidTokenError: On a string, boolean or non-finite value.
    """
    for name in _NUMERIC_CLAIMS:
        if name not in claims:
            continue
        value = claims[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise jwt.InvalidTokenError(f"{name} must be a number")
        if not math.isfinite(value):
            raise jwt.InvalidTokenError(f"{name} must be finite")


def as_signing_keys(keys: SessionSigningKeys | bytes) -> SessionSigningKeys:
    """Accept a bare cookie secret where signing keys are expected (HS256 only)."""
    return keys if isinstance(keys, SessionSigningKeys) else SessionSigningKeys.hs256(keys)


def provider_signing_keys(auth_provider: UnifiedAuthProvider) -> SessionSigningKeys:
    """The signing keys of an ``oidc`` or ``accounts`` provider."""
    config = (
        auth_provider._oidc_config
        if auth_provider._source == "oidc"
        else auth_provider._accounts_config
    )
    if config is None:
        raise RuntimeError(f"{auth_provider._source!r} auth has no session signing keys")
    return config.signing_keys


def _read_pem(inline_env: str, file_env: str) -> bytes | None:
    inline = os.environ.get(inline_env, "").strip()
    path = os.environ.get(file_env, "").strip()
    if inline and path:
        raise SessionSigningConfigError(f"set {inline_env} or {file_env}, not both")
    if inline:
        # Env files often carry a PEM on one line with literal "\n".
        return inline.replace("\\n", "\n").encode("ascii", errors="strict")
    if path:
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            raise SessionSigningConfigError(
                f"{file_env}={path!r} is not readable: {exc.strerror}"
            ) from None
    return None


def _load_private(pem: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        raise SessionSigningConfigError(
            f"the {KEY_ENV}[_FILE] value is not an unencrypted PEM private key"
        ) from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SessionSigningConfigError(
            f"the {KEY_ENV}[_FILE] key is {type(key).__name__}, not Ed25519"
        )
    return key


def _load_public(pem: bytes) -> PublicSigningKey:
    try:
        key = serialization.load_pem_public_key(pem)
    except (ValueError, TypeError):
        raise SessionSigningConfigError(
            f"the {PREVIOUS_KEY_ENV}[_FILE] value is not a PEM public key "
            "(`openssl pkey -in old.pem -pubout`)"
        ) from None
    if not isinstance(key, Ed25519PublicKey):
        raise SessionSigningConfigError(
            f"the {PREVIOUS_KEY_ENV}[_FILE] key is {type(key).__name__}, not Ed25519"
        )
    return PublicSigningKey.of(key)


def _parse_legacy_until(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.combine(
                date.fromisoformat(raw), datetime.min.time(), tzinfo=timezone.utc
            )
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise SessionSigningConfigError(
            f"{LEGACY_UNTIL_ENV} must be YYYY-MM-DD or an ISO datetime; got {raw!r}"
        ) from None
    if parsed.tzinfo is None:
        raise SessionSigningConfigError(
            f"{LEGACY_UNTIL_ENV} datetime needs a UTC offset; got {raw!r}"
        )
    return parsed


def with_session_keys(config: _ConfigT, default_issuer: str) -> _ConfigT:
    """Return *config* carrying the keys :meth:`SessionSigningKeys.from_env` builds."""
    keys = SessionSigningKeys.from_env(config.cookie_secret, default_issuer=default_issuer)
    return dataclasses.replace(config, session_keys=keys)
