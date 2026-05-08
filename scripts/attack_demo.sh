#!/usr/bin/env bash
# Trip Booking Concierge — every attack vector is rejected.
# This is the artifact you show people. The pytest suite proves it programmatically;
# this script makes the rejection visible.

set -euo pipefail

cd "$(dirname "$0")/.."

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
fi
if ! grep -q "^APPROVAL_SIGNING_KEY=." .env 2>/dev/null; then
  "$PY" -c "from approval.tokens import generate_keypair; generate_keypair()" >/dev/null
fi

rm -f .trip-cli-state.json data/trip.db data/trip.db-* 2>/dev/null || true

echo "============================================================"
echo " ATTACK DEMO — every attempt is REJECTED with audit entries."
echo "============================================================"
echo
echo "[1/5] Plan a fresh trip..."
"$PY" -m cli.trip plan "4 days in Lisbon next month under \$2000" >/dev/null
OPT_ID=$("$PY" -c "import json; d=json.load(open('.trip-cli-state.json')); print(d['options'][0]['id'])")
echo "  selected option: $OPT_ID"
echo

echo "[2/5] Attack: authorize WITHOUT selecting first..."
set +e
OUTPUT=$("$PY" -m cli.trip authorize "$OPT_ID" "000000" 2>&1)
RC=$?
set -e
echo "$OUTPUT" | head -3
echo "  -> exit code $RC (expected != 0)"
[ $RC -ne 0 ] && echo "  ✓ REJECTED as expected" || (echo "  ✗ FAIL: should have failed"; exit 1)
echo

echo "[3/5] Attack: select then submit WRONG OTP code..."
SELECT_OUT=$("$PY" -m cli.trip select "$OPT_ID")
set +e
OUTPUT=$("$PY" -m cli.trip authorize "$OPT_ID" "000000" 2>&1)
RC=$?
set -e
echo "$OUTPUT" | head -3
[ $RC -ne 0 ] && echo "  ✓ REJECTED as expected" || (echo "  ✗ FAIL: should have failed"; exit 1)
echo

echo "[4/5] Attack: token replay — book once, then try again with same token..."
OTP=$(echo "$SELECT_OUT" | grep -oE "code: [0-9]{6}" | grep -oE "[0-9]{6}" | head -1)
"$PY" -m cli.trip authorize "$OPT_ID" "$OTP" >/dev/null
echo "  first booking succeeded; replaying same OTP..."
set +e
OUTPUT=$("$PY" -m cli.trip authorize "$OPT_ID" "$OTP" 2>&1)
RC=$?
set -e
echo "$OUTPUT" | head -3
[ $RC -ne 0 ] && echo "  ✓ REJECTED — pending consumed" || (echo "  ✗ FAIL: should have failed"; exit 1)
echo

echo "[5/5] Attack: tamper with the audit log directly..."
"$PY" -c "
import sqlite3
import json
conn = sqlite3.connect('data/trip.db')
conn.execute(\"UPDATE events SET payload = ? WHERE seq = 2\", (json.dumps({'tampered': True}, sort_keys=True, separators=(',', ':')),))
conn.commit()
print('  tampered seq=2 payload')
"
set +e
OUTPUT=$("$PY" -m cli.trip verify-chain 2>&1)
RC=$?
set -e
echo "$OUTPUT"
[ $RC -ne 0 ] && echo "  ✓ TAMPERING DETECTED" || (echo "  ✗ FAIL: chain should be broken"; exit 1)
echo

echo "============================================================"
echo " ALL 5 ATTACKS REJECTED. The audit log records every refusal."
echo "============================================================"
