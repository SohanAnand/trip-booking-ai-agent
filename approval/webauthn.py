"""WebAuthn-shaped passkey flow.

Server-side implementation that mirrors the WebAuthn dance:
  - generate_authentication_options(...) → challenge (32 random bytes,
    base64url) bound to (option_id, request_id, user_id).
  - verify_authentication_response(...) → checks that the assertion's
    clientDataJSON contains our challenge, the signature is valid against the
    user's registered public key, and counter is monotonically increasing.

For M4 we use Ed25519 (same primitive as approval/tokens.py) and a simplified
clientDataJSON. A production product should switch to py_webauthn so we get
attestation-format coverage and CTAP2 compatibility for free.

Pending challenges are stored in SQLite (table: webauthn_challenges).
Public keys are stored in user_passkeys (one row per registered credential).
"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass

import nacl.exceptions
import nacl.signing

from approval.tokens import b64u_decode, b64u_encode
from audit.log import sha256_hex
from tools.flights.amadeus import _settings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS webauthn_challenges (
    challenge   TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    option_id   TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    issued_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_passkeys (
    credential_id TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    public_key    TEXT NOT NULL,
    sign_count    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_settings().sqlite_path)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


CHALLENGE_TTL = 90    # seconds


@dataclass
class AuthenticationOptions:
    challenge: str           # base64url
    rp_id: str               # relying-party id (your domain)
    user_id: str
    option_id: str
    timeout_ms: int = 60_000


def generate_authentication_options(
    *,
    user_id: str,
    request_id: str,
    option_id: str,
    rp_id: str = "localhost",
) -> AuthenticationOptions:
    challenge = b64u_encode(secrets.token_bytes(32))
    with _conn() as conn:
        conn.execute(
            "INSERT INTO webauthn_challenges (challenge, user_id, option_id, request_id, issued_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (challenge, user_id, option_id, request_id, int(time.time())),
        )
    return AuthenticationOptions(
        challenge=challenge, rp_id=rp_id, user_id=user_id, option_id=option_id,
    )


@dataclass
class AuthenticationResponse:
    """Mirrors the WebAuthn AuthenticatorAssertionResponse shape (simplified)."""
    credential_id: str
    client_data_json: str    # base64url-encoded JSON
    signature: str           # base64url-encoded signature over (clientDataJSON || authData)
    authenticator_data: str  # base64url
    sign_count: int


class WebAuthnFailed(Exception):
    pass


def verify_authentication_response(
    *,
    user_id: str,
    option_id: str,
    response: AuthenticationResponse,
) -> str:
    """Verify the assertion. Returns the consumed challenge on success."""
    client_data = json.loads(base64.urlsafe_b64decode(
        _pad(response.client_data_json)
    ))
    challenge = client_data.get("challenge")
    origin = client_data.get("origin", "")
    type_ = client_data.get("type", "")
    if type_ != "webauthn.get":
        raise WebAuthnFailed(f"unexpected clientData.type: {type_}")
    if not origin.startswith(("http://localhost", "http://127.0.0.1", "https://")):
        raise WebAuthnFailed(f"untrusted origin: {origin}")

    with _conn() as conn:
        ch = conn.execute(
            "SELECT * FROM webauthn_challenges WHERE challenge = ?",
            (challenge,),
        ).fetchone()
        if not ch:
            raise WebAuthnFailed("unknown challenge (replay or unknown)")
        if ch["user_id"] != user_id or ch["option_id"] != option_id:
            raise WebAuthnFailed("challenge bound to different (user, option)")
        if int(time.time()) - ch["issued_at"] > CHALLENGE_TTL:
            conn.execute("DELETE FROM webauthn_challenges WHERE challenge = ?", (challenge,))
            raise WebAuthnFailed("challenge expired")

        # Single-use: delete now to prevent replay.
        conn.execute("DELETE FROM webauthn_challenges WHERE challenge = ?", (challenge,))

        cred = conn.execute(
            "SELECT * FROM user_passkeys WHERE credential_id = ? AND user_id = ?",
            (response.credential_id, user_id),
        ).fetchone()
        if not cred:
            raise WebAuthnFailed("unknown credential_id")

        if response.sign_count <= cred["sign_count"]:
            raise WebAuthnFailed("sign_count not monotonically increasing — possible cloned key")

        vk = nacl.signing.VerifyKey(b64u_decode(cred["public_key"]))
        signed = (
            base64.urlsafe_b64decode(_pad(response.authenticator_data))
            + sha256_hex(json.dumps(client_data, separators=(",", ":"))).encode()
        )
        try:
            vk.verify(signed, b64u_decode(response.signature))
        except nacl.exceptions.BadSignatureError as e:
            raise WebAuthnFailed("signature verification failed") from e

        # Update sign_count.
        conn.execute(
            "UPDATE user_passkeys SET sign_count = ? WHERE credential_id = ?",
            (response.sign_count, response.credential_id),
        )
    return challenge


def register_passkey(*, user_id: str, credential_id: str, public_key_b64u: str) -> None:
    """Convenience helper — would normally be called after the WebAuthn registration
    flow (`generate_registration_options` → `verify_registration_response`)."""
    from datetime import UTC, datetime
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_passkeys "
            "(credential_id, user_id, public_key, sign_count, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (credential_id, user_id, public_key_b64u, datetime.now(UTC).isoformat()),
        )


def _pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)
