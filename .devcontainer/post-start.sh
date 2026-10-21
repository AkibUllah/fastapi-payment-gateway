#!/usr/bin/env bash
# Codespaces start hook: bring up API + PostgreSQL and seed one demo merchant.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d --build

echo "waiting for the API on :8000 ..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if [ ! -f .demo-merchant.txt ]; then
  docker compose exec -T api python -m app.cli create-merchant "Dhaka Mart" \
    --webhook-url https://example.com/hooks | tee .demo-merchant.txt
fi

cat <<MSG

Payment gateway is up.
  API docs:        forwarded port 8000, path /docs
  Demo merchant:   see .demo-merchant.txt (X-API-Key value is the api_key line)
  Smoke run:       API_KEY=<api_key> ./scripts/demo.sh
MSG
