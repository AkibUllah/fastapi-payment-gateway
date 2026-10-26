"""Idempotency-Key support.

A merchant may send the same request twice (network retry, double click). The first
response is stored against (merchant, key, request fingerprint) and replayed verbatim.
Reusing a key with a different body is rejected, because silently returning the old
response would hide a client bug.

Keys are reserved before any work happens. The first request inserts an
``in_progress`` row and commits it straight away, so the unique constraint on
(merchant, key) decides the winner even when two requests arrive at the same moment.
A duplicate that arrives while the first is still running gets
:class:`IdempotencyInProgressError` (HTTP 409 with ``Retry-After``) instead of reaching
the payment provider a second time. If the request that holds the key crashes, the
reservation's lease (``locked_until``) runs out and the next retry takes it over.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IdempotencyRecord

IN_PROGRESS = "in_progress"
COMPLETED = "completed"


class IdempotencyConflictError(Exception):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Idempotency-Key '{key}' was already used with a different request body")


class IdempotencyInProgressError(Exception):
    """Another request with the same key is still being processed."""

    def __init__(self, key: str, retry_after: int) -> None:
        self.key = key
        self.retry_after = retry_after
        super().__init__(
            f"A request with Idempotency-Key '{key}' is still in progress; retry in {retry_after}s"
        )


def fingerprint(payload: Any) -> str:
    """Stable SHA-256 of a JSON-serialisable payload (key order independent)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; treat them as UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Reservation:
    """Outcome of :meth:`IdempotencyStore.begin`.

    Exactly one of ``record_id`` (this request now owns the key) or ``replay`` (a stored
    response to return as-is) is set.
    """

    record_id: int | None = None
    replay: IdempotencyRecord | None = None


class IdempotencyStore:
    def __init__(
        self,
        db: Session,
        ttl_hours: int = 24,
        lock_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._ttl = timedelta(hours=ttl_hours)
        self._lock = timedelta(seconds=lock_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _find(self, merchant_id: str, key: str) -> IdempotencyRecord | None:
        return self._db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.merchant_id == merchant_id,
                IdempotencyRecord.key == key,
            )
        )

    def lookup(self, merchant_id: str, key: str, request_hash: str) -> IdempotencyRecord | None:
        """Return the stored record for a completed request, or ``None``."""
        record = self._find(merchant_id, key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(key)
        return record if record.state == COMPLETED else None

    def begin(self, merchant_id: str, key: str, request_hash: str) -> Reservation:
        """Reserve ``key`` for this request, or explain why it can't be.

        Commits the session: the reservation has to be visible to concurrent requests
        before any provider call is made.
        """
        for _ in range(2):
            now = self._clock()
            record = self._find(merchant_id, key)
            if record is None:
                self._db.add(
                    IdempotencyRecord(
                        merchant_id=merchant_id,
                        key=key,
                        request_hash=request_hash,
                        state=IN_PROGRESS,
                        locked_until=now + self._lock,
                        created_at=now,
                    )
                )
                try:
                    self._db.commit()
                except IntegrityError:
                    # Lost the race to a request that inserted the same key first; look
                    # again so we report what that request is doing.
                    self._db.rollback()
                    continue
                return Reservation(record_id=self._find(merchant_id, key).id)  # type: ignore[union-attr]

            if record.request_hash != request_hash:
                raise IdempotencyConflictError(key)
            if record.state == COMPLETED:
                return Reservation(replay=record)

            locked_until = _utc(record.locked_until) if record.locked_until else now
            if locked_until > now:
                raise IdempotencyInProgressError(
                    key, retry_after=max(1, math.ceil((locked_until - now).total_seconds()))
                )
            # The holder's lease ran out (it crashed or timed out). Take the key over, but
            # only if nobody else did in the meantime.
            taken = self._db.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.id == record.id,
                    IdempotencyRecord.state == IN_PROGRESS,
                    IdempotencyRecord.locked_until == record.locked_until,
                )
                .values(locked_until=now + self._lock)
            )
            self._db.commit()
            if taken.rowcount == 1:
                return Reservation(record_id=record.id)
            raise IdempotencyInProgressError(key, retry_after=1)
        raise IdempotencyInProgressError(key, retry_after=1)

    def complete(self, record_id: int, status_code: int, body: dict[str, Any]) -> None:
        """Store the response; the caller commits it together with the business change."""
        self._db.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == record_id)
            .values(
                state=COMPLETED,
                status_code=status_code,
                response_body=json.dumps(body),
                locked_until=None,
            )
        )

    def release(self, record_id: int) -> None:
        """Drop a reservation whose request failed, so the client can retry the same key."""
        self._db.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.id == record_id, IdempotencyRecord.state == IN_PROGRESS
            )
        )
        self._db.commit()

    def save(
        self,
        merchant_id: str,
        key: str,
        request_hash: str,
        status_code: int,
        body: dict[str, Any],
    ) -> IdempotencyRecord:
        """Insert a completed record directly (used by tests and data imports)."""
        record = IdempotencyRecord(
            merchant_id=merchant_id,
            key=key,
            request_hash=request_hash,
            state=COMPLETED,
            status_code=status_code,
            response_body=json.dumps(body),
        )
        self._db.add(record)
        self._db.flush()
        return record

    def purge_expired(self, now: datetime | None = None) -> int:
        current = now or self._clock()
        cutoff = current - self._ttl
        result = self._db.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff)
        )
        return int(result.rowcount or 0)
