"""Audit archive: signed JSONL export of a day's events."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import nacl.signing
import pytest

from approval.tokens import b64u_decode
from audit.archive import archive_day
from audit.log import jcs_canonical


@pytest.mark.asyncio
async def test_archive_writes_signed_manifest(log, tmp_path, monkeypatch):
    # Append a few events
    log.append("user", "request.opened", {"raw_text": "Lisbon"}, request_id="r1")
    log.append("agent", "tool.called", {"tool": "search_flights"}, request_id="r1")

    # Run the archive into a tmp dir.
    monkeypatch.chdir(tmp_path)
    today = datetime.now(UTC).date()
    p = archive_day(today)
    assert p.exists()

    # Manifest content
    with open(p, encoding="utf-8") as f:
        manifest_doc = json.load(f)
    manifest = manifest_doc["manifest"]
    assert manifest["events_count"] == 2
    assert manifest["date"] == today.isoformat()
    assert manifest["terminal_hash"]   # non-empty

    # Verify signature using the public verify key from the test env.
    from api.config import settings
    vk = nacl.signing.VerifyKey(b64u_decode(settings.approval_verify_key))
    canonical = jcs_canonical(manifest).encode()
    # If signature is invalid, this raises.
    vk.verify(canonical, b64u_decode(manifest_doc["sig"]))

    # The JSONL file should exist and contain 2 lines.
    jsonl = (tmp_path / "audit_archive" / f"{today.isoformat()}.jsonl")
    lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
