"""Outgoing merchant webhooks: queued in the same transaction as the state change, sent after
commit by a background task, retried on a durable schedule, signed with the merchant's secret."""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.backoff import DEFAULT_RETRY_SCHEDULE, next_delay
from app.core.security import build_signature_header
from app.models import Merchant, Payment, WebhookDelivery, utc_now
from app.schemas import PaymentOut
from app.services.webhook_queue import Claim, DeliveryQueue

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Gateway-Signature"
EVENT_HEADER = "X-Gateway-Event"
DELIVERY_HEADER = "X-Gateway-Delivery"


def build_event_payload(delivery_id: str, event_type: str, payment: Payment) -> dict:
    return {
        "id": delivery_id,
        "type": event_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": PaymentOut.from_model(payment, include_history=False).model_dump(mode="json"),
    }


@dataclass(frozen=True)
class AttemptResult:
    """Outcome of one POST to a merchant endpoint."""

    status_code: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class WebhookOutbox:
    """Collects deliveries during a request so they can be sent after commit.

    Each row is committed with the state change and carries its own `next_attempt_at`, so if
    the process dies before the background task runs, a dispatcher sends it instead. The grace
    period just keeps the dispatcher from racing the in-request send in the normal case.
    """

    def __init__(
        self,
        db: Session,
        *,
        grace_seconds: float = 60,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._db = db
        self._grace = timedelta(seconds=grace_seconds)
        self._clock = clock
        self.pending: list[str] = []

    def enqueue(
        self, merchant: Merchant, payment: Payment, event_type: str
    ) -> WebhookDelivery | None:
        if not merchant.webhook_url:
            return None
        delivery = WebhookDelivery(
            merchant_id=merchant.id,
            payment_id=payment.id,
            event_type=event_type,
            target_url=merchant.webhook_url,
            payload="{}",
            next_attempt_at=self._clock() + self._grace,
        )
        self._db.add(delivery)
        self._db.flush()
        delivery.payload = json.dumps(build_event_payload(delivery.id, event_type, payment))
        self._db.flush()
        self.pending.append(delivery.id)
        return delivery


class WebhookSender:
    """HTTP delivery on top of `DeliveryQueue`. `transport` is injectable for tests."""

    def __init__(
        self,
        timeout: float = 5.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        retry_schedule: Sequence[int] = DEFAULT_RETRY_SCHEDULE,
        jitter: float = 0.1,
        lease_seconds: float = 60,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = backoff_seconds
        self.retry_schedule = tuple(retry_schedule)
        self.jitter = jitter
        self.lease_seconds = lease_seconds
        self.transport = transport
        self.clock = clock
        self._sleep = sleep
        self._rng = rng

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    def _queue(self, session_factory: sessionmaker[Session]) -> DeliveryQueue:
        return DeliveryQueue(session_factory, lease_seconds=self.lease_seconds, clock=self.clock)

    def deliver(self, session_factory: sessionmaker[Session], delivery_ids: Sequence[str]) -> None:
        """The in-request fast path. What it cannot deliver is left for the dispatcher."""
        if not delivery_ids:
            return
        queue = self._queue(session_factory)
        with self._client() as client:
            for delivery_id in delivery_ids:
                claim = queue.claim_by_id(delivery_id)
                if claim is not None:
                    self._send(queue, client, claim, self.max_attempts, retries_used=0)

    def _send(
        self,
        queue: DeliveryQueue,
        client: httpx.Client,
        claim: Claim,
        attempts: int,
        retries_used: int,
    ) -> None:
        """Run up to `attempts` POSTs for a claim, then record how it ended."""
        result = None
        for attempt in range(1, attempts + 1):
            if not queue.start_attempt(claim):
                logger.warning("webhook lease lost id=%s, leaving it to the new owner", claim.id)
                return
            result = self._post(client, claim)
            if result.ok:
                queue.mark_delivered(claim, result.status_code)
                logger.info(
                    "webhook delivered id=%s event=%s status=%s attempt=%s",
                    claim.id,
                    claim.event_type,
                    result.status_code,
                    attempt,
                )
                return
            logger.warning(
                "webhook attempt failed id=%s attempt=%s error=%s", claim.id, attempt, result.error
            )
            if attempt < attempts and self.backoff_seconds:
                self._sleep(self.backoff_seconds * attempt)

        delay = next_delay(self.retry_schedule, retries_used, self.jitter, self._rng)
        next_attempt_at = None if delay is None else self.clock() + timedelta(seconds=delay)
        recorded = queue.record_failure(
            claim,
            status_code=result.status_code,
            error=result.error,
            retry_count=retries_used,
            next_attempt_at=next_attempt_at,
        )
        if recorded and next_attempt_at is None:
            logger.error("webhook gave up id=%s error=%s", claim.id, result.error)

    @staticmethod
    def _post(client: httpx.Client, claim: Claim) -> AttemptResult:
        """One signed POST. The signature is rebuilt per call so a late retry stays inside
        the receiver's timestamp tolerance."""
        body = claim.payload.encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "fastapi-payment-gateway/0.1",
            SIGNATURE_HEADER: build_signature_header(claim.webhook_secret, body),
            EVENT_HEADER: claim.event_type,
            DELIVERY_HEADER: claim.id,
        }
        try:
            response = client.post(claim.target_url, content=body, headers=headers)
        except Exception as exc:  # e.g. httpx.InvalidURL: one bad row must not stop the worker
            return AttemptResult(None, f"{type(exc).__name__}: {exc}"[:500])
        if 200 <= response.status_code < 300:
            return AttemptResult(response.status_code)
        return AttemptResult(response.status_code, f"HTTP {response.status_code}")


def build_webhook_sender(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> WebhookSender:
    return WebhookSender(
        timeout=settings.webhook_timeout_seconds,
        max_attempts=settings.webhook_max_attempts,
        backoff_seconds=settings.webhook_retry_backoff_seconds,
        retry_schedule=settings.webhook_retry_steps,
        jitter=settings.webhook_retry_jitter,
        lease_seconds=settings.webhook_lease_seconds,
        transport=transport,
    )
