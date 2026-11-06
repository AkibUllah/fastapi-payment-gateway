"""Durable queue over `webhook_deliveries`: who may send a row, and when.

A worker claims a row in a short transaction, sends with no transaction open, then records the
result. Row locks only arbitrate the claim, so a slow merchant endpoint never pins a database
connection. Ownership during the send lives in the row itself as a lease (`locked_until`) plus
a fencing token (`claim_token`), which is what lets it survive a crash:

- A worker that dies mid-send stops renewing, and the row is claimable once the lease expires.
- A worker that overruns its lease cannot start another attempt or overwrite the new owner.
- A late "delivered" is still honoured: the merchant did acknowledge the event, and recording
  it stops the new owner from sending it again.

Delivery is at-least-once. A crash between the merchant's 2xx and the write below resends the
event, so receivers deduplicate on the `X-Gateway-Delivery` header.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import ColumnElement

from app.models import Merchant, WebhookDelivery, utc_now

PENDING = "pending"  # queued, not sent yet
RETRYING = "retrying"  # at least one failed attempt, waiting for `next_attempt_at`
DELIVERED = "delivered"  # terminal
FAILED = "failed"  # terminal: the schedule ran out (a manual retry can reopen it)

ACTIVE_STATUSES = (PENDING, RETRYING)


@dataclass(frozen=True)
class Claim:
    """Everything needed to send a delivery, detached from any session."""

    id: str
    token: str
    event_type: str
    target_url: str
    payload: str
    webhook_secret: str
    retry_count: int


class DeliveryQueue:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: float = 60,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._lease = timedelta(seconds=lease_seconds)
        self._clock = clock

    def claim_due(self) -> Claim | None:
        """Claim the most overdue row, skipping rows another worker is claiming right now."""
        now = self._clock()
        return self._claim(now, WebhookDelivery.next_attempt_at <= now)

    def claim_by_id(self, delivery_id: str) -> Claim | None:
        """Claim one row for an in-request send: never sent, or brought forward to now.

        A row the dispatcher got to first and rescheduled is left to its schedule.
        """
        now = self._clock()
        return self._claim(
            now,
            WebhookDelivery.id == delivery_id,
            or_(WebhookDelivery.attempts == 0, WebhookDelivery.next_attempt_at <= now),
        )

    def _claim(self, now: datetime, *criteria: ColumnElement[bool]) -> Claim | None:
        free = (
            WebhookDelivery.status.in_(ACTIVE_STATUSES),
            or_(WebhookDelivery.locked_until.is_(None), WebhookDelivery.locked_until <= now),
        )
        with self._sessions() as db:
            row = db.scalar(
                select(WebhookDelivery)
                .where(*free, *criteria)
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            token = uuid.uuid4().hex
            # SQLite ignores FOR UPDATE, so the conditional UPDATE is what makes the claim
            # atomic there. On PostgreSQL the row lock already guarantees it.
            won = db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == row.id, *free)
                .values(claim_token=token, locked_until=now + self._lease)
                .execution_options(synchronize_session=False)
            )
            if won.rowcount != 1:
                db.rollback()
                return None
            secret = db.scalar(
                select(Merchant.webhook_secret).where(Merchant.id == row.merchant_id)
            )
            claim = Claim(
                id=row.id,
                token=token,
                event_type=row.event_type,
                target_url=row.target_url,
                payload=row.payload,
                webhook_secret=secret,
                retry_count=row.retry_count,
            )
            db.commit()
            return claim

    def start_attempt(self, claim: Claim) -> bool:
        """Count an attempt and renew the lease. False means the lease was lost: do not send."""
        return self._update_owned(
            claim,
            attempts=WebhookDelivery.attempts + 1,
            locked_until=self._clock() + self._lease,
        )

    def mark_delivered(self, claim: Claim, status_code: int | None) -> None:
        with self._sessions() as db:
            db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == claim.id)
                .values(
                    status=DELIVERED,
                    delivered_at=self._clock(),
                    response_status=status_code,
                    last_error=None,
                    next_attempt_at=None,
                    locked_until=None,
                    claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()

    def record_failure(
        self,
        claim: Claim,
        *,
        status_code: int | None,
        error: str | None,
        retry_count: int,
        next_attempt_at: datetime | None,
    ) -> bool:
        """Release the row for its next retry, or fail it when `next_attempt_at` is None.

        Returns False if the lease was lost, in which case the new owner decides.
        """
        return self._update_owned(
            claim,
            status=RETRYING if next_attempt_at else FAILED,
            response_status=status_code,
            last_error=error,
            retry_count=retry_count,
            next_attempt_at=next_attempt_at,
            locked_until=None,
            claim_token=None,
        )

    def _update_owned(self, claim: Claim, **values: object) -> bool:
        with self._sessions() as db:
            result = db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == claim.id, WebhookDelivery.claim_token == claim.token)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            return result.rowcount == 1
