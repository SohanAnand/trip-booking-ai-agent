"""Shared test fixtures.

Each test function gets a fresh AuditLog backed by a per-test SQLite file,
plus a freshly-generated Ed25519 keypair so tokens don't leak across tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Per-test SQLite file + ephemeral Ed25519 keys."""
    import nacl.signing
    from approval.tokens import b64u_encode

    db_path = str(tmp_path / "trip.db")
    sk = nacl.signing.SigningKey.generate()
    vk = sk.verify_key
    monkeypatch.setenv("SQLITE_PATH", db_path)
    monkeypatch.setenv("APPROVAL_SIGNING_KEY", b64u_encode(bytes(sk)))
    monkeypatch.setenv("APPROVAL_VERIFY_KEY", b64u_encode(bytes(vk)))
    monkeypatch.setenv("APPROVAL_KID", "test-v1")
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("DEMO_USER_ID", "test-user")

    # Force tests to use mocks unless they explicitly opt in by setting these.
    # Without this, the auto-detecting orchestrator would pick up keys from .env.
    for key in (
        "DUFFEL_ACCESS_TOKEN", "AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET",
        "LITEAPI_KEY", "OPENWEATHER_API_KEY", "VOYAGE_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    # Reload settings & default audit log singleton with the new env.
    import importlib
    import api.config
    importlib.reload(api.config)
    import audit.log
    importlib.reload(audit.log)

    yield


@pytest.fixture
def log():
    from audit.log import get_default_log
    return get_default_log()
