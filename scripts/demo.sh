#!/usr/bin/env bash
# Trip Booking Concierge — M1 happy-path demo.
# Plans Lisbon, picks option 2, authorizes, books, verifies the chain.

set -euo pipefail

cd "$(dirname "$0")/.."

# Resolve the project Python: prefer the venv interpreter so we don't trip
# over the Microsoft Store `python` alias on Windows.
if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi
export PYTHONIOENCODING=utf-8

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[demo] copied .env.example to .env"
fi

# Generate signing keys if missing
if ! grep -q "^APPROVAL_SIGNING_KEY=." .env 2>/dev/null; then
  echo "[demo] generating Ed25519 keys..."
  "$PY" -c "from approval.tokens import generate_keypair; generate_keypair()" >/dev/null
fi

# Reset CLI state and DB for a clean run
rm -f .trip-cli-state.json data/trip.db data/trip.db-* 2>/dev/null || true

echo
echo "============================================================"
echo " 1. Plan a trip"
echo "============================================================"
"$PY" -m cli.trip plan "4 days in Lisbon next month under \$2000"

# Pick option 2 (best_reviewed) — read its option_id from state
OPT_ID=$("$PY" -c "import json; d=json.load(open('.trip-cli-state.json')); print(d['options'][1]['id'])")

echo
echo "============================================================"
echo " 2. Select option 2 ($OPT_ID)"
echo "============================================================"
SELECT_OUT=$("$PY" -m cli.trip select "$OPT_ID")
echo "$SELECT_OUT"

# Extract the OTP from output (the demo prints it; production sends via SMS)
OTP=$(echo "$SELECT_OUT" | grep -oE "code: [0-9]{6}" | grep -oE "[0-9]{6}" | head -1)
if [ -z "$OTP" ]; then
  echo "[demo] could not read OTP from select output" >&2
  exit 1
fi

echo
echo "============================================================"
echo " 3. Authorize with OTP $OTP"
echo "============================================================"
"$PY" -m cli.trip authorize "$OPT_ID" "$OTP"

echo
echo "============================================================"
echo " 4. Verify audit chain"
echo "============================================================"
"$PY" -m cli.trip verify-chain

echo
echo "============================================================"
echo " 5. Show event timeline"
echo "============================================================"
"$PY" -m cli.trip events

echo
echo "[demo] DONE."
