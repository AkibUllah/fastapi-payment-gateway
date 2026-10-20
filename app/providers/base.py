"""The PaymentProvider protocol and shared result types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ProviderStatus(str, Enum):
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ProviderError(Exception):
    """Transport or protocol failure talking to a provider (not a decline)."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"{provider}: {message}")


@dataclass(frozen=True)
class ProviderResult:
    status: ProviderStatus
    provider_ref: str
    message: str | None = None
    redirect_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is not ProviderStatus.FAILED


@dataclass(frozen=True)
class ProviderWebhookEvent:
    """A normalised inbound notification from a provider."""

    event_type: str  # payment.authorized | payment.captured | payment.failed | payment.refunded
    provider_ref: str
    amount_minor: int | None = None
    currency: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderReportRow:
    """One line of a provider's daily settlement report."""

    provider_ref: str
    amount_minor: int
    fee_minor: int
    currency: str
    settled_on: date


@runtime_checkable
class PaymentProvider(Protocol):
    name: str
    display_name: str
    supported_currencies: frozenset[str]
    supported_methods: frozenset[str]

    def supports(self, currency: str, method: str) -> bool: ...

    def authorize(
        self,
        *,
        payment_id: str,
        amount_minor: int,
        currency: str,
        method: str,
        auto_capture: bool,
        metadata: Mapping[str, Any],
    ) -> ProviderResult: ...

    def capture(self, provider_ref: str, amount_minor: int) -> ProviderResult: ...

    def refund(
        self, provider_ref: str, amount_minor: int, reason: str | None
    ) -> ProviderResult: ...

    def cancel(self, provider_ref: str) -> ProviderResult: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> None: ...

    def parse_webhook(self, body: bytes) -> ProviderWebhookEvent: ...

    def fetch_report(self, settled_on: date) -> list[ProviderReportRow]: ...
