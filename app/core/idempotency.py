"""Idempotency-Key support.

A merchant may send the same request twice (network retry, double click). The first
response is stored against (merchant, key, request fingerprint) and replayed verbatim.
Reusing a key with a different body is rejected, because silently returning the old
response would hide a client bug.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import IdempotencyRecord


class IdempotencyConflictError(Exception):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Idempotency-Key '{key}' was already used with a different request body")


def fingerprint(payload: Any) -> str:
    """Stable SHA-256 of a JSON-serialisable payload (key order independent)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyStore:
    def __init__(self, db: Session, ttl_hours: int = 24) -> None:
        self._db = db
        self._ttl = timedelta(hours=ttl_hours)

    def lookup(self, merchant_id: str, key: str, request_hash: str) -> IdempotencyRecord | None:
        record = self._db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.merchant_id == merchant_id,
                IdempotencyRecord.key == key,
            )
        )
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(key)
        return record

    def save(
        self,
        merchant_id: str,
        key: str,
        request_hash: str,
        status_code: int,
        body: dict[str, Any],
    ) -> IdempotencyRecord:
        record = IdempotencyRecord(
            merchant_id=merchant_id,
            key=key,
            request_hash=request_hash,
            status_code=status_code,
            response_body=json.dumps(body),
        )
        self._db.add(record)
        self._db.flush()
        return record

    def purge_expired(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        cutoff = current - self._ttl
        result = self._db.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff)
        )
        return int(result.rowcount or 0)
