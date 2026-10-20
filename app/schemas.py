"""Pydantic request/response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.money import CURRENCY_EXPONENTS, format_amount
from app.models import Payment, PaymentEvent, Refund, Settlement, SettlementLine, WebhookDelivery

CaptureMode = Literal["automatic", "manual"]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class PaymentCreate(BaseModel):
    amount_minor: int = Field(gt=0, le=10**12, description="Amount in minor units (paisa/cents)")
    currency: str = Field(min_length=3, max_length=3, examples=["BDT"])
    method: str = Field(min_length=2, max_length=32, examples=["bkash", "card"])
    provider: str | None = Field(default=None, description="Force a provider instead of routing")
    capture: CaptureMode = "automatic"
    customer_ref: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, value: str) -> str:
        upper = value.upper()
        if upper not in CURRENCY_EXPONENTS:
            raise ValueError(f"unsupported currency {upper}")
        return upper

    @field_validator("method")
    @classmethod
    def _method_lower(cls, value: str) -> str:
        return value.lower()

    @field_validator("metadata")
    @classmethod
    def _metadata_size(cls, value: dict) -> dict:
        if len(value) > 20:
            raise ValueError("metadata may hold at most 20 keys")
        return value


class CaptureRequest(BaseModel):
    amount_minor: int | None = Field(default=None, gt=0)


class RefundRequest(BaseModel):
    amount_minor: int | None = Field(default=None, gt=0, description="Omit for a full refund")
    reason: str | None = Field(default=None, max_length=500)


class PaymentEventOut(BaseModel):
    from_status: str | None
    to_status: str
    source: str
    note: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, event: PaymentEvent) -> PaymentEventOut:
        return cls(
            from_status=event.from_status,
            to_status=event.to_status,
            source=event.source,
            note=event.note,
            created_at=event.created_at,
        )


class RefundOut(BaseModel):
    id: str
    payment_id: str
    amount_minor: int
    amount: str
    status: str
    provider_ref: str | None
    reason: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, refund: Refund, currency: str) -> RefundOut:
        return cls(
            id=refund.id,
            payment_id=refund.payment_id,
            amount_minor=refund.amount_minor,
            amount=format_amount(refund.amount_minor, currency),
            status=refund.status,
            provider_ref=refund.provider_ref,
            reason=refund.reason,
            created_at=refund.created_at,
        )


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    provider: str
    provider_ref: str | None
    amount_minor: int
    amount: str
    currency: str
    method: str
    fee_minor: int
    net_minor: int
    refunded_minor: int
    refundable_minor: int
    capture_mode: str
    customer_ref: str | None
    description: str | None
    metadata: dict[str, Any]
    redirect_url: str | None
    failure_reason: str | None
    settlement_id: str | None
    created_at: datetime
    updated_at: datetime
    captured_at: datetime | None
    events: list[PaymentEventOut] = Field(default_factory=list)
    refunds: list[RefundOut] = Field(default_factory=list)

    @classmethod
    def from_model(cls, payment: Payment, include_history: bool = True) -> PaymentOut:
        return cls(
            id=payment.id,
            status=payment.status,
            provider=payment.provider,
            provider_ref=payment.provider_ref,
            amount_minor=payment.amount_minor,
            amount=format_amount(payment.amount_minor, payment.currency),
            currency=payment.currency,
            method=payment.method,
            fee_minor=payment.fee_minor,
            net_minor=payment.amount_minor - payment.fee_minor - payment.refunded_minor,
            refunded_minor=payment.refunded_minor,
            refundable_minor=payment.refundable_minor,
            capture_mode=payment.capture_mode,
            customer_ref=payment.customer_ref,
            description=payment.description,
            metadata=dict(payment.metadata_json or {}),
            redirect_url=payment.redirect_url,
            failure_reason=payment.failure_reason,
            settlement_id=payment.settlement_id,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
            captured_at=payment.captured_at,
            events=[PaymentEventOut.from_model(e) for e in payment.events]
            if include_history
            else [],
            refunds=[RefundOut.from_model(r, payment.currency) for r in payment.refunds]
            if include_history
            else [],
        )


class RefundResponse(BaseModel):
    refund: RefundOut
    payment: PaymentOut


class WebhookAck(BaseModel):
    provider: str
    event_type: str
    provider_ref: str
    result: Literal["applied", "ignored", "unknown_reference", "conflict"]
    payment_id: str | None = None


class SettlementRunRequest(BaseModel):
    settlement_date: date | None = Field(default=None, description="Defaults to yesterday (UTC)")
    providers: list[str] | None = None


class SettlementLineOut(BaseModel):
    payment_id: str
    provider_ref: str | None
    gross_minor: int
    fee_minor: int
    refunded_minor: int
    net_minor: int

    @classmethod
    def from_model(cls, line: SettlementLine) -> SettlementLineOut:
        return cls(
            payment_id=line.payment_id,
            provider_ref=line.provider_ref,
            gross_minor=line.gross_minor,
            fee_minor=line.fee_minor,
            refunded_minor=line.refunded_minor,
            net_minor=line.net_minor,
        )


class SettlementOut(BaseModel):
    id: str
    provider: str
    currency: str
    settlement_date: date
    gross_minor: int
    fee_minor: int
    refunded_minor: int
    net_minor: int
    net: str
    payment_count: int
    status: str
    created_at: datetime
    reconciled_at: datetime | None
    lines: list[SettlementLineOut] = Field(default_factory=list)

    @classmethod
    def from_model(cls, settlement: Settlement, include_lines: bool = False) -> SettlementOut:
        return cls(
            id=settlement.id,
            provider=settlement.provider,
            currency=settlement.currency,
            settlement_date=settlement.settlement_date,
            gross_minor=settlement.gross_minor,
            fee_minor=settlement.fee_minor,
            refunded_minor=settlement.refunded_minor,
            net_minor=settlement.net_minor,
            net=format_amount(settlement.net_minor, settlement.currency),
            payment_count=settlement.payment_count,
            status=settlement.status,
            created_at=settlement.created_at,
            reconciled_at=settlement.reconciled_at,
            lines=[SettlementLineOut.from_model(line) for line in settlement.lines]
            if include_lines
            else [],
        )


class ProviderReportRowIn(BaseModel):
    provider_ref: str
    amount_minor: int = Field(ge=0)
    fee_minor: int = Field(ge=0, default=0)


class ReconcileRequest(BaseModel):
    rows: list[ProviderReportRowIn] | None = Field(
        default=None,
        description="Provider report rows. Omit to pull the report from the provider adapter.",
    )


class ReconciliationMismatch(BaseModel):
    provider_ref: str
    ledger_amount_minor: int | None
    provider_amount_minor: int | None
    ledger_fee_minor: int | None
    provider_fee_minor: int | None


class ReconciliationResult(BaseModel):
    settlement_id: str
    matched: int
    amount_mismatches: list[ReconciliationMismatch]
    missing_in_provider: list[str]
    unexpected_in_provider: list[str]
    ledger_net_minor: int
    provider_net_minor: int
    reconciled: bool


class WebhookDeliveryOut(BaseModel):
    id: str
    payment_id: str
    event_type: str
    target_url: str
    status: str
    attempts: int
    response_status: int | None
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None

    @classmethod
    def from_model(cls, delivery: WebhookDelivery) -> WebhookDeliveryOut:
        return cls(
            id=delivery.id,
            payment_id=delivery.payment_id,
            event_type=delivery.event_type,
            target_url=delivery.target_url,
            status=delivery.status,
            attempts=delivery.attempts,
            response_status=delivery.response_status,
            last_error=delivery.last_error,
            created_at=delivery.created_at,
            delivered_at=delivery.delivered_at,
        )


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: Literal["ok", "error"]
    providers: list[str]
