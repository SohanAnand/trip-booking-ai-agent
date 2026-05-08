"""WebAuthn-shaped passkey flow.

Verifies:
  - happy path: register → generate_options → assert → verify_response.
  - replay: same challenge consumed twice fails.
  - bound option_id: assertion for option A can't satisfy a challenge for option B.
  - sign_count monotonic: cloned-credential detection.
"""

from __future__ import annotations

import base64
import hashlib
import json

import nacl.signing
import pytest

from approval.tokens import b64u_decode, b64u_encode
from approval.webauthn import (
    AuthenticationResponse,
    WebAuthnFailed,
    generate_authentication_options,
    register_passkey,
    verify_authentication_response,
)


def _build_assertion(*, user_id: str, option_id: str, challenge: str,
                     sk: nacl.signing.SigningKey, sign_count: int,
                     credential_id: str,
                     origin: str = "http://localhost:3000") -> AuthenticationResponse:
    client_data = {
        "type": "webauthn.get",
        "challenge": challenge,
        "origin": origin,
    }
    client_data_str = json.dumps(client_data, separators=(",", ":"))
    auth_data = b"\x00" * 37    # WebAuthn 'authenticatorData' bytes (placeholder)
    cd_hash = hashlib.sha256(client_data_str.encode()).hexdigest().encode()
    signed = auth_data + cd_hash
    sig = sk.sign(signed).signature
    return AuthenticationResponse(
        credential_id=credential_id,
        client_data_json=b64u_encode(client_data_str.encode()),
        signature=b64u_encode(sig),
        authenticator_data=b64u_encode(auth_data),
        sign_count=sign_count,
    )


def test_happy_path():
    sk = nacl.signing.SigningKey.generate()
    cred_id = "cred-1"
    register_passkey(
        user_id="u-1", credential_id=cred_id,
        public_key_b64u=b64u_encode(bytes(sk.verify_key)),
    )
    opts = generate_authentication_options(
        user_id="u-1", request_id="r-1", option_id="o-A",
    )
    assertion = _build_assertion(
        user_id="u-1", option_id="o-A", challenge=opts.challenge,
        sk=sk, sign_count=1, credential_id=cred_id,
    )
    consumed = verify_authentication_response(
        user_id="u-1", option_id="o-A", response=assertion,
    )
    assert consumed == opts.challenge


def test_replay_rejected():
    sk = nacl.signing.SigningKey.generate()
    register_passkey(user_id="u-2", credential_id="cred-2",
                     public_key_b64u=b64u_encode(bytes(sk.verify_key)))
    opts = generate_authentication_options(
        user_id="u-2", request_id="r-2", option_id="o-A",
    )
    a1 = _build_assertion(user_id="u-2", option_id="o-A", challenge=opts.challenge,
                           sk=sk, sign_count=1, credential_id="cred-2")
    verify_authentication_response(user_id="u-2", option_id="o-A", response=a1)
    a2 = _build_assertion(user_id="u-2", option_id="o-A", challenge=opts.challenge,
                           sk=sk, sign_count=2, credential_id="cred-2")
    with pytest.raises(WebAuthnFailed, match="unknown challenge"):
        verify_authentication_response(user_id="u-2", option_id="o-A", response=a2)


def test_challenge_bound_to_option():
    sk = nacl.signing.SigningKey.generate()
    register_passkey(user_id="u-3", credential_id="cred-3",
                     public_key_b64u=b64u_encode(bytes(sk.verify_key)))
    opts = generate_authentication_options(
        user_id="u-3", request_id="r-3", option_id="o-A",
    )
    a = _build_assertion(user_id="u-3", option_id="o-A", challenge=opts.challenge,
                          sk=sk, sign_count=1, credential_id="cred-3")
    # Challenge was minted for option o-A; trying to verify against o-B fails.
    with pytest.raises(WebAuthnFailed, match="bound to different"):
        verify_authentication_response(user_id="u-3", option_id="o-B", response=a)


def test_clone_detection_via_sign_count():
    sk = nacl.signing.SigningKey.generate()
    register_passkey(user_id="u-4", credential_id="cred-4",
                     public_key_b64u=b64u_encode(bytes(sk.verify_key)))
    opts1 = generate_authentication_options(
        user_id="u-4", request_id="r-4", option_id="o-A",
    )
    a1 = _build_assertion(user_id="u-4", option_id="o-A", challenge=opts1.challenge,
                           sk=sk, sign_count=5, credential_id="cred-4")
    verify_authentication_response(user_id="u-4", option_id="o-A", response=a1)

    opts2 = generate_authentication_options(
        user_id="u-4", request_id="r-4", option_id="o-A",
    )
    # New assertion claims sign_count=3, less than the stored 5 → cloned key
    a2 = _build_assertion(user_id="u-4", option_id="o-A", challenge=opts2.challenge,
                           sk=sk, sign_count=3, credential_id="cred-4")
    with pytest.raises(WebAuthnFailed, match="monotonically"):
        verify_authentication_response(user_id="u-4", option_id="o-A", response=a2)
