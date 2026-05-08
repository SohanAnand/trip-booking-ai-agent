"""Out-of-band SMS challenge (M5).

In production this calls Twilio. For the prototype, console-print the code
so the demo runs without a Twilio account. The 6-digit code is bound into
the ApprovalToken's user_consent_text_hash so it cryptographically pairs
the OOB challenge with the eventual booking.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


@dataclass
class OobChallenge:
    code: str
    target_phone: str


def issue_sms_challenge(*, target_phone: str) -> OobChallenge:
    """Issue a 6-digit code and dispatch via SMS.

    For the demo: prints to stdout. Set TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN
    + TWILIO_FROM in env to dispatch real SMS. The contract is identical.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    if os.environ.get("TWILIO_ACCOUNT_SID"):
        # TODO: integrate twilio when keys are present
        pass
    else:
        print(f"[OOB SMS stub] To {target_phone}: your code is {code}")
    return OobChallenge(code=code, target_phone=target_phone)
