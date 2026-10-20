"""Outgoing merchant webhooks: queued in the same transaction as the state change,
delivered after commit by a background task, signed with the merchant's secret."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
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
        body = delivery.payload.encode("utf-8")
        for attempt in range(1, self.max_attempts + 1):
            delivery.attempts = attempt
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "fastapi-payment-gateway/0.1",
                SIGNATURE_HEADER: build_signature_header(merchant.webhook_secret, body),
                EVENT_HEADER: delivery.event_type,
                DELIVERY_HEADER: delivery.id,
            }
            try:
                response = client.post(delivery.target_url, content=body, headers=headers)
                delivery.response_status = response.status_code
                if 200 <= response.status_code < 300:
                    delivery.status = "delivered"
                    delivery.delivered_at = datetime.now(timezone.utc)
                    delivery.last_error = None
                    logger.info(
                        "webhook delivered id=%s event=%s status=%s attempt=%s",
                        delivery.id,
                        delivery.event_type,
                        response.status_code,
                        attempt,
                    )
                    return
                delivery.last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                delivery.response_status = None
                delivery.last_error = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning(
                "webhook attempt failed id=%s attempt=%s error=%s",
                delivery.id,
                attempt,
                delivery.last_error,
            )
            if attempt < self.max_attempts and self.backoff_seconds:
                self._sleep(self.backoff_seconds * attempt)
        delivery.status = "failed"
