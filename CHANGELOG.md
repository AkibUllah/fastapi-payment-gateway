# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-26

### Added

- Idempotency keys are reserved before the provider is called. Concurrent duplicates of a request now get `409 idempotency_key_in_progress` with a `Retry-After` header instead of reaching the provider a second time. A request that fails releases its key; one that crashes frees it when `IDEMPOTENCY_LOCK_SECONDS` (default 60) runs out.
- CI job that runs the concurrent-reservation test against PostgreSQL.

- CodeQL code scanning workflow and Dependabot configuration (pip, Docker, GitHub Actions).
- API reference and terminal screenshots in the README.
- Roadmap tracked as GitHub issues under the `v0.2.0` milestone.

### Changed

- `idempotency_keys` gains `state` and `locked_until` columns, and `status_code` / `response_body` become nullable. Existing databases need:
  ```sql
  ALTER TABLE idempotency_keys ADD COLUMN state VARCHAR(16) NOT NULL DEFAULT 'completed';
  ALTER TABLE idempotency_keys ADD COLUMN locked_until TIMESTAMPTZ;
  ALTER TABLE idempotency_keys ALTER COLUMN status_code DROP NOT NULL;
  ALTER TABLE idempotency_keys ALTER COLUMN response_body DROP NOT NULL;
  ```

- CI workflow runs with a read-only `GITHUB_TOKEN` by default; Dependabot groups all GitHub Actions bumps into one PR.

## [0.1.0] - 2026-10-25

First public release.

### Added

- One payment API in front of sandbox bKash, SSLCommerz and Stripe adapters behind a single `PaymentProvider` protocol, plus a `MockProvider` for tests.
- Payment state machine (`created → authorized → captured → partially_refunded/refunded`, `failed`, `cancelled`) with illegal transitions rejected as HTTP 409.
- `Idempotency-Key` support on create, capture and refund with byte-for-byte replay and body-mismatch rejection.
- Hashed merchant API keys with per-merchant data isolation.
- HMAC-SHA256 signed merchant webhooks via a transactional outbox with bounded retries and a queryable delivery log.
- Inbound provider webhook verification (bKash/SSLCommerz HMAC, Stripe-style timestamped signatures) applied idempotently.
- Fee-aware routing in basis points with half-up integer rounding; full and partial refunds and partial captures.
- Daily settlement batches per provider and currency, CSV export, reconciliation against provider reports and a paid close-out.
- CLI (`create-merchant`, `run-settlement`, `purge-idempotency`), Docker image, docker compose with PostgreSQL, CI on Python 3.10/3.11.
- Live API reference on GitHub Pages, Codespaces and Render deploy kits.

### Fixed

- Validation errors are serialised correctly in the 422 error envelope.
- `postgres://` and `postgresql://` database URLs are rewritten to the psycopg 3 driver.

[Unreleased]: https://github.com/AkibUllah/fastapi-payment-gateway/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/AkibUllah/fastapi-payment-gateway/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AkibUllah/fastapi-payment-gateway/releases/tag/v0.1.0
