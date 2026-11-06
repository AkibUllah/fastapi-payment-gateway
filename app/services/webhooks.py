"""Outgoing merchant webhooks: queued in the same transaction as the state change,
delivered after commit by a background task, signed with the merchant's secret."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import build_signature_header
from app.models import Merchant, Payment, WebhookDelivery
from app.schemas import PaymentOut

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
    """Collects deliveries during a request so they can be dispatched after commit."""

    def __init__(self, db: Session) -> None:
        self._db = db
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
        )
        self._db.add(delivery)
        self._db.flush()
        delivery.payload = json.dumps(build_event_payload(delivery.id, event_type, payment))
        self._db.flush()
        self.pending.append(delivery.id)
        return delivery


class WebhookSender:
    """HTTP delivery with bounded retries. `transport` is injectable for tests."""

    def __init__(
        self,
        timeout: float = 5.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = backoff_seconds
        self.transport = transport
        self._sleep = sleep

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    def deliver(self, session_factory: sessionmaker[Session], delivery_ids: Sequence[str]) -> None:
        if not delivery_ids:
            return
        with session_factory() as db:
            deliveries = db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.id.in_(list(delivery_ids)))
            ).all()
            merchants = {
                m.id: m
                for m in db.scalars(
                    select(Merchant).where(Merchant.id.in_({d.merchant_id for d in deliveries}))
                )
            }
            with self._client() as client:
                for delivery in deliveries:
                    self._deliver_one(client, delivery, merchants[delivery.merchant_id])
            db.commit()

    def _deliver_one(
        self, client: httpx.Client, delivery: WebhookDelivery, merchant: Merchant
    ) -> None:
        for attempt in range(1, self.max_attempts + 1):
            delivery.attempts = attempt
            result = self._post(
                client,
                target_url=delivery.target_url,
                payload=delivery.payload,
                event_type=delivery.event_type,
                delivery_id=delivery.id,
                secret=merchant.webhook_secret,
            )
            delivery.response_status = result.status_code
            delivery.last_error = result.error
            if result.ok:
                delivery.status = "delivered"
                delivery.delivered_at = datetime.now(timezone.utc)
                logger.info(
                    "webhook delivered id=%s event=%s status=%s attempt=%s",
                    delivery.id,
                    delivery.event_type,
                    result.status_code,
                    attempt,
                )
                return
            logger.warning(
                "webhook attempt failed id=%s attempt=%s error=%s",
                delivery.id,
                attempt,
                result.error,
            )
            if attempt < self.max_attempts and self.backoff_seconds:
                self._sleep(self.backoff_seconds * attempt)
        delivery.status = "failed"

    @staticmethod
    def _post(
        client: httpx.Client,
        *,
        target_url: str,
        payload: str,
        event_type: str,
        delivery_id: str,
        secret: str,
    ) -> AttemptResult:
        """One signed POST. The signature is rebuilt per call so a late retry stays inside
        the receiver's timestamp tolerance."""
        body = payload.encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "fastapi-payment-gateway/0.1",
            SIGNATURE_HEADER: build_signature_header(secret, body),
            EVENT_HEADER: event_type,
            DELIVERY_HEADER: delivery_id,
        }
        try:
            response = client.post(target_url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return AttemptResult(None, f"{type(exc).__name__}: {exc}"[:500])
        if 200 <= response.status_code < 300:
            return AttemptResult(response.status_code)
        return AttemptResult(response.status_code, f"HTTP {response.status_code}")
