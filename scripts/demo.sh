#!/usr/bin/env bash
# End-to-end smoke run against a local server on :8000.
# Usage: API_KEY=pk_test_... ./scripts/demo.sh
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
: "${API_KEY:?set API_KEY to a merchant key from 'gateway create-merchant'}"

echo "# health"; curl -s "$BASE/health"; echo

echo "# create bKash payment (auto-capture), idempotent"
PAYMENT=$(curl -s -X POST "$BASE/v1/payments" \
  -H "X-API-Key: $API_KEY" -H "Idempotency-Key: demo-$(date +%s)" -H "Content-Type: application/json" \
  -d '{"amount_minor":150000,"currency":"BDT","method":"bkash","customer_ref":"01711000000","description":"Order #4521"}')
echo "$PAYMENT"; echo
ID=$(echo "$PAYMENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "# partial refund"
curl -s -X POST "$BASE/v1/payments/$ID/refund" -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"amount_minor":50000,"reason":"one item returned"}'; echo

echo "# settle today"
curl -s -X POST "$BASE/v1/settlements/run" -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d "{\"settlement_date\":\"$(date -u +%F)\"}"; echo
