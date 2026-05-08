"""M1: hardcoded demo user. M4 wires WebAuthn passkeys."""

from __future__ import annotations

from api.config import settings


def current_user_id() -> str:
    return settings.demo_user_id
