"""PaymentService: the single place that mutates payment state."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.routing import NoRouteError, PaymentRouter
from app.core.state_machine import (
    REFUNDABLE_STATES,
    InvalidTransitionError,
    PaymentStatus,
    assert_transition,
    is_terminal,
    refund_target_status,
)
from app.models import Merchant, Payment, PaymentEvent, Refund
from app.providers.base import (
    PaymentProvider,
    ProviderError,
    ProviderStatus,
    ProviderWebhookEvent,
)
from app.schemas import PaymentCreate
from app.services.errors import (
    ConflictError,
    NotFoundError,
    ProviderUnavailable,
    ValidationFailed,
)
from app.services.webhooks import WebhookOutbox

logger = logging.getLogger(__name__)

_PROVIDER_STATUS_TO_PAYMENT = {
    ProviderStatus.AUTHORIZED: PaymentStatus.AUTHORIZED,
    ProviderStatus.CAPTURED: PaymentStatus.CAPTURED,
    ProviderStatus.FAILED: PaymentStatus.FAILED,
    ProviderStatus.CANCELLED: PaymentStatus.CANCELLED,
}

_EVENT_TO_STATUS = {
    "payment.authorized": PaymentStatus.AUTHORIZED,
    "payment.captured": PaymentStatus.CAPTURED,
    "payment.failed": PaymentStatus.FAILED,
    "payment.cancelled": PaymentStatus.CANCELLED,
}


class PaymentService:
    def __init__(
        self,
        db: Session,
        router: PaymentRouter,
        providers: Mapping[str, PaymentProvider],
        outbox: WebhookOutbox,
    ) -> None:
        self.db = db
        self.router = router
        self.providers = providers
        self.outbox = outbox

    # -- queries -------------------------------------------------------------------
    def get_payment(self, merchant: Merchant, payment_id: str) -> Payment:
        payment = self.db.scalar(
            select(Payment)
            .options(selectinload(Payment.events), selectinload(Payment.refunds))
            .where(Payment.id == payment_id, Payment.merchant_id == merchant.id)
        )
        if payment is None:
            raise NotFoundError(f"Payment {payment_id} not found")
        return payment

    def list_payments(self, merchant: Merchant, status: str | None, limit: int, offset: int):
        stmt = (
            select(Payment)
            .where(Payment.merchant_id == merchant.id)
            .order_by(Payment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status:
            stmt = stmt.where(Payment.status == status)
        return self.db.scalars(stmt).all()

    # -- commands ------------------------------------------------------------------
    def create_payment(self, merchant: Merchant, data: PaymentCreate) -> Payment:
        try:
            quote = self.router.route(data.amount_minor, data.currency, data.method, data.provider)
        except NoRouteError as exc:
            raise ValidationFailed(str(exc)) from exc
        provider = self.providers[quote.provider]

        payment = Payment(
            merchant_id=merchant.id,
            provider=provider.name,
            status=PaymentStatus.CREATED.value,
            amount_minor=data.amount_minor,
            currency=data.currency,
            method=data.method,
            fee_minor=quote.fee_minor,
            capture_mode=data.capture,
            customer_ref=data.customer_ref,
            description=data.description,
            metadata_json=dict(data.metadata),
        )
        self.db.add(payment)
        self.db.flush()
        self._record_event(
            payment, None, PaymentStatus.CREATED, "api", f"routed to {provider.name}"
        )

        try:
            result = provider.authorize(
                payment_id=payment.id,
                amount_minor=payment.amount_minor,
                currency=payment.currency,
                method=payment.method,
                auto_capture=data.capture == "automatic",
                metadata=data.metadata,
            )
        except ProviderError as exc:
            logger.exception("provider error during authorize payment=%s", payment.id)
            self._transition(payment, PaymentStatus.FAILED, "system", str(exc), merchant)
            payment.failure_reason = str(exc)
            raise ProviderUnavailable(str(exc)) from exc

        payment.provider_ref = result.provider_ref
        payment.redirect_url = result.redirect_url
        target = _PROVIDER_STATUS_TO_PAYMENT[result.status]
        if target is PaymentStatus.FAILED:
            payment.failure_reason = result.message
        self._transition(payment, target, "provider", result.message, merchant)
        self.db.flush()
        return self.get_payment(merchant, payment.id)

    def capture(self, merchant: Merchant, payment_id: str, amount_minor: int | None) -> Payment:
        payment = self.get_payment(merchant, payment_id)
        current = PaymentStatus(payment.status)
        if current is not PaymentStatus.AUTHORIZED:
            raise ConflictError(
                f"Payment is {current.value}; only authorized payments can be captured"
            )
        capture_amount = amount_minor if amount_minor is not None else payment.amount_minor
        if capture_amount > payment.amount_minor:
            raise ValidationFailed("Capture amount exceeds the authorised amount")
        provider = self.providers[payment.provider]
        result = self._call(lambda: provider.capture(payment.provider_ref or "", capture_amount))
        if result.status is ProviderStatus.FAILED:
            raise ConflictError(result.message or "Provider refused the capture")
        if capture_amount != payment.amount_minor:
            # Partial capture: the uncaptured remainder is released; fee is re-quoted.
            rule = self.router.fee_rule(payment.provider, payment.currency)
            payment.amount_minor = capture_amount
            payment.fee_minor = rule.compute(capture_amount) if rule else payment.fee_minor
        self._transition(payment, PaymentStatus.CAPTURED, "api", None, merchant)
        self.db.flush()
        return payment

    def cancel(self, merchant: Merchant, payment_id: str) -> Payment:
        payment = self.get_payment(merchant, payment_id)
        current = PaymentStatus(payment.status)
        try:
            assert_transition(current, PaymentStatus.CANCELLED)
        except InvalidTransitionError as exc:
            raise ConflictError(str(exc)) from exc
        if payment.provider_ref and current is PaymentStatus.AUTHORIZED:
            provider = self.providers[payment.provider]
            self._call(lambda: provider.cancel(payment.provider_ref or ""))
        self._transition(payment, PaymentStatus.CANCELLED, "api", None, merchant)
        self.db.flush()
        return payment

    def refund(
        self,
        merchant: Merchant,
        payment_id: str,
        amount_minor: int | None,
        reason: str | None,
    ) -> tuple[Payment, Refund]:
        payment = self.get_payment(merchant, payment_id)
        current = PaymentStatus(payment.status)
        if current not in REFUNDABLE_STATES:
            raise ConflictError(f"Payment is {current.value}; nothing to refund")
        refund_amount = amount_minor if amount_minor is not None else payment.refundable_minor
        if refund_amount <= 0:
            raise ValidationFailed("Refund amount must be positive")
        if refund_amount > payment.refundable_minor:
            raise ValidationFailed(
                f"Refund of {refund_amount} exceeds refundable balance {payment.refundable_minor}"
            )
        provider = self.providers[payment.provider]
        result = self._call(
            lambda: provider.refund(payment.provider_ref or "", refund_amount, reason)
        )
        if result.status is ProviderStatus.FAILED:
            raise ConflictError(result.message or "Provider refused the refund")

        refund = Refund(
            payment_id=payment.id,
            amount_minor=refund_amount,
            status="succeeded",
            provider_ref=result.provider_ref,
            reason=reason,
            source="api",
        )
        self.db.add(refund)
        payment.refunded_minor += refund_amount
        target = refund_target_status(payment.amount_minor, payment.refunded_minor)
        self._transition(payment, target, "api", f"refund {refund_amount}", merchant)
        self.db.flush()
        self.db.refresh(payment)
        return payment, refund

    # -- inbound provider notifications -------------------------------------------
    def apply_provider_event(
        self, provider_name: str, event: ProviderWebhookEvent
    ) -> tuple[str, Payment | None]:
        payment = self.db.scalar(
            select(Payment)
            .options(selectinload(Payment.merchant), selectinload(Payment.events))
            .where(Payment.provider == provider_name, Payment.provider_ref == event.provider_ref)
        )
        if payment is None:
            return "unknown_reference", None
        current = PaymentStatus(payment.status)
        merchant = payment.merchant

        if event.event_type == "payment.refunded":
            if event.amount_minor is None or event.amount_minor <= payment.refunded_minor:
                return "ignored", payment
            delta = event.amount_minor - payment.refunded_minor
            if delta > payment.refundable_minor or current not in REFUNDABLE_STATES:
                return "conflict", payment
            self.db.add(
                Refund(
                    payment_id=payment.id,
                    amount_minor=delta,
                    status="succeeded",
                    provider_ref=str(event.raw.get("id") or event.provider_ref),
                    reason="provider-initiated refund",
                    source="provider",
                )
            )
            payment.refunded_minor += delta
            target = refund_target_status(payment.amount_minor, payment.refunded_minor)
            self._transition(payment, target, "provider", event.event_type, merchant)
            self.db.flush()
            return "applied", payment

        target = _EVENT_TO_STATUS.get(event.event_type)
        if target is None or target is current:
            return "ignored", payment
        if is_terminal(current) or not _can(current, target):
            logger.warning(
                "ignoring provider event %s for payment %s in state %s",
                event.event_type,
                payment.id,
                current.value,
            )
            return "conflict", payment
        if event.amount_minor is not None and event.amount_minor != payment.amount_minor:
            logger.warning("amount mismatch on provider event payment=%s", payment.id)
            return "conflict", payment
        if target is PaymentStatus.FAILED:
            payment.failure_reason = str(event.raw.get("reason") or "reported failed by provider")
        self._transition(payment, target, "provider", event.event_type, merchant)
        self.db.flush()
        return "applied", payment

    # -- internals -----------------------------------------------------------------
    def _call(self, fn):
        try:
            return fn()
        except ProviderError as exc:
            raise ProviderUnavailable(str(exc)) from exc

    def _transition(
        self,
        payment: Payment,
        target: PaymentStatus,
        source: str,
        note: str | None,
        merchant: Merchant | None,
    ) -> None:
        current = PaymentStatus(payment.status)
        try:
            assert_transition(current, target)
        except InvalidTransitionError as exc:
            raise ConflictError(str(exc)) from exc
        payment.status = target.value
        now = datetime.now(timezone.utc)
        if target is PaymentStatus.AUTHORIZED:
            payment.authorized_at = now
        if target is PaymentStatus.CAPTURED:
            payment.authorized_at = payment.authorized_at or now
            payment.captured_at = now
        self._record_event(payment, current, target, source, note)
        if merchant is not None:
            self.outbox.enqueue(merchant, payment, f"payment.{target.value}")

    def _record_event(
        self,
        payment: Payment,
        from_status: PaymentStatus | None,
        to_status: PaymentStatus,
        source: str,
        note: str | None,
    ) -> None:
        self.db.add(
            PaymentEvent(
                payment_id=payment.id,
                from_status=from_status.value if from_status else None,
                to_status=to_status.value,
                source=source,
                note=note,
            )
        )
        self.db.flush()


def _can(current: PaymentStatus, target: PaymentStatus) -> bool:
    try:
        assert_transition(current, target)
    except InvalidTransitionError:
        return False
    return True
