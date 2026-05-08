"""Ed25519-signed ApprovalToken.

The token is the cryptographic proof that:
  - the user explicitly authorized this booking,
  - against this exact itinerary option (option_hash),
  - at this exact amount,
  - with a single-use jti (replay defense),
  - within a short TTL window.

Booking service rejects every charge that does not carry a valid token.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import nacl.signing
from nacl.exceptions import BadSignatureError

from audit.log import jcs_canonical, sha256_hex

TOKEN_VERSION = 1
DEFAULT_TTL_SECONDS = 90


def b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass
class ApprovalTokenPayload:
    v: int
    jti: str
    sub: str            # user_id
    aud: str            # "payment-service"
    iss: str            # "approval-service"
    iat: int
    nbf: int
    exp: int
    request_id: str
    option_id: str
    option_hash: str
    idempotency_key: str
    amount_value: str   # string, not float
    amount_currency: str
    payment_method_id: str
    user_consent_text_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "jti": self.jti,
            "sub": self.sub,
            "aud": self.aud,
            "iss": self.iss,
            "iat": self.iat,
            "nbf": self.nbf,
            "exp": self.exp,
            "request_id": self.request_id,
            "option_id": self.option_id,
            "option_hash": self.option_hash,
            "idempotency_key": self.idempotency_key,
            "amount_value": self.amount_value,
            "amount_currency": self.amount_currency,
            "payment_method_id": self.payment_method_id,
            "user_consent_text_hash": self.user_consent_text_hash,
        }


@dataclass
class ApprovalToken:
    payload: ApprovalTokenPayload
    sig: str   # base64url
    kid: str

    def to_dict(self) -> dict:
        return {"payload": self.payload.to_dict(), "sig": self.sig, "kid": self.kid}


# ----- key management ---------------------------------------------------------

def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair and write to .env (or print).

    Returns (signing_b64u, verify_b64u).
    """
    sk = nacl.signing.SigningKey.generate()
    vk = sk.verify_key
    sk_b = b64u_encode(bytes(sk))
    vk_b = b64u_encode(bytes(vk))

    env_path = ".env"
    written = False
    if os.path.exists(env_path):
        with open(env_path, "r+", encoding="utf-8") as f:
            content = f.read()
            new_lines = []
            sk_set = vk_set = False
            for line in content.splitlines():
                if line.startswith("APPROVAL_SIGNING_KEY="):
                    new_lines.append(f"APPROVAL_SIGNING_KEY={sk_b}")
                    sk_set = True
                elif line.startswith("APPROVAL_VERIFY_KEY="):
                    new_lines.append(f"APPROVAL_VERIFY_KEY={vk_b}")
                    vk_set = True
                else:
                    new_lines.append(line)
            if not sk_set:
                new_lines.append(f"APPROVAL_SIGNING_KEY={sk_b}")
            if not vk_set:
                new_lines.append(f"APPROVAL_VERIFY_KEY={vk_b}")
            f.seek(0)
            f.write("\n".join(new_lines) + "\n")
            f.truncate()
            written = True

    print(f"APPROVAL_SIGNING_KEY={sk_b}")
    print(f"APPROVAL_VERIFY_KEY={vk_b}")
    if written:
        print(f"\nKeys written to {env_path}.")
    return sk_b, vk_b


def _signing_key() -> nacl.signing.SigningKey:
    from api.config import settings
    if not settings.approval_signing_key:
        raise RuntimeError(
            "APPROVAL_SIGNING_KEY not set. Run `make keys` to generate one."
        )
    return nacl.signing.SigningKey(b64u_decode(settings.approval_signing_key))


def _verify_key() -> nacl.signing.VerifyKey:
    from api.config import settings
    if not settings.approval_verify_key:
        raise RuntimeError(
            "APPROVAL_VERIFY_KEY not set. Run `make keys` to generate one."
        )
    return nacl.signing.VerifyKey(b64u_decode(settings.approval_verify_key))


# ----- signing & verification -------------------------------------------------

def derive_idempotency_key(jti: str, option_hash: str, payment_method_id: str) -> str:
    return sha256_hex(f"{jti}|{option_hash}|{payment_method_id}")


def consent_text_hash(text: str) -> str:
    return sha256_hex(text)


def mint_token(
    *,
    user_id: str,
    request_id: str,
    option_id: str,
    option_hash: str,
    amount_value: str,
    amount_currency: str,
    payment_method_id: str,
    user_consent_text: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> ApprovalToken:
    from api.config import settings
    now = int(time.time())
    jti = str(uuid.uuid4())
    idempotency_key = derive_idempotency_key(jti, option_hash, payment_method_id)
    payload = ApprovalTokenPayload(
        v=TOKEN_VERSION,
        jti=jti,
        sub=user_id,
        aud="payment-service",
        iss="approval-service",
        iat=now,
        nbf=now,
        exp=now + ttl_seconds,
        request_id=request_id,
        option_id=option_id,
        option_hash=option_hash,
        idempotency_key=idempotency_key,
        amount_value=amount_value,
        amount_currency=amount_currency,
        payment_method_id=payment_method_id,
        user_consent_text_hash=consent_text_hash(user_consent_text),
    )
    canonical = jcs_canonical(payload.to_dict())
    sig = _signing_key().sign(canonical.encode("utf-8")).signature
    return ApprovalToken(payload=payload, sig=b64u_encode(sig), kid=settings.approval_kid)


class TokenInvalid(Exception):
    """Raised whenever a token fails verification."""


def verify_token(
    token: ApprovalToken | dict,
    *,
    expected_aud: str = "payment-service",
    expected_option_id: str | None = None,
    expected_option_hash: str | None = None,
    expected_amount_value: str | None = None,
    expected_amount_currency: str | None = None,
    now_ts: int | None = None,
) -> ApprovalTokenPayload:
    """Verify all token invariants. Raises TokenInvalid on any failure.

    NOTE: replay (jti consumption) is enforced separately at the booking call site
    via AuditLog.consume_jti — this function is a pure cryptographic + claims check.
    """
    from api.config import settings

    if isinstance(token, dict):
        token = ApprovalToken(
            payload=ApprovalTokenPayload(**token["payload"]),
            sig=token["sig"],
            kid=token["kid"],
        )

    if token.payload.v != TOKEN_VERSION:
        raise TokenInvalid(f"unsupported version {token.payload.v}")
    if token.kid != settings.approval_kid:
        raise TokenInvalid(f"unknown kid {token.kid}")
    if token.payload.aud != expected_aud:
        raise TokenInvalid(f"audience mismatch (got {token.payload.aud})")

    now = now_ts if now_ts is not None else int(time.time())
    if now < token.payload.nbf:
        raise TokenInvalid("token not yet valid (nbf in future)")
    if now > token.payload.exp:
        raise TokenInvalid("token expired")

    canonical = jcs_canonical(token.payload.to_dict()).encode("utf-8")
    try:
        _verify_key().verify(canonical, b64u_decode(token.sig))
    except BadSignatureError as e:
        raise TokenInvalid("signature invalid") from e

    # Bind claims
    if expected_option_id and token.payload.option_id != expected_option_id:
        raise TokenInvalid(
            f"option_id mismatch (token={token.payload.option_id}, expected={expected_option_id})"
        )
    if expected_option_hash and token.payload.option_hash != expected_option_hash:
        raise TokenInvalid("option_hash mismatch — option mutated since signing")
    if expected_amount_value is not None and token.payload.amount_value != expected_amount_value:
        raise TokenInvalid("amount_value mismatch")
    if expected_amount_currency is not None and token.payload.amount_currency != expected_amount_currency:
        raise TokenInvalid("amount_currency mismatch")

    # Recompute and verify idempotency_key
    expected_idem = derive_idempotency_key(
        token.payload.jti, token.payload.option_hash, token.payload.payment_method_id
    )
    if token.payload.idempotency_key != expected_idem:
        raise TokenInvalid("idempotency_key not derived from (jti, option_hash, pm_id)")

    return token.payload
