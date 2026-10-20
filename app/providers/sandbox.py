"""Shared in-memory sandbox behaviour for the built-in adapters.

Each adapter keeps its own ledger of references so capture/refund/report calls behave
consistently within a process. The sandbox deliberately has no network access; swapping
in a real adapter means implementing the same PaymentProvider protocol over HTTP.

Outcome control (sandbox only): pass `metadata={"sandbox_outcome": "fail"}` to make
`authorize` return a decline, which exercises the FAILED path end to end.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from app.core.money import FeeRule
from app.providers.base import (
    ProviderError,
    ProviderReportRow,
    ProviderResult,
    ProviderStatus,
    ProviderWebhookEvent,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SandboxRecord:
    provider_ref: str
    amount_minor: int
    currency: str
    status: ProviderStatus
    captured_minor: int = 0
    refunded_minor: int = 0
    captured_on: date | None = None


class SandboxProvider:
    """Base class implementing the PaymentProvider protocol against an in-memory ledger."""

    name: str = "sandbox"
    display_name: str = "Sandbox"
    supported_currencies: frozenset[str] = frozenset()
    supported_methods: frozenset[str] = frozenset()
    ref_prefix: str = "SBX"
    report_fee_rule: FeeRule = FeeRule(percent_bps=0)

    def __init__(self, webhook_secret: str, clock: Clock = utc_now) -> None:
        self.webhook_secret = webhook_secret
        self._clock = clock
        self._records: dict[str, SandboxRecord] = {}

    # -- helpers -------------------------------------------------------------------
    def _new_ref(self) -> str:
        return f"{self.ref_prefix}{secrets.token_hex(5).upper()}"

    def _get(self, provider_ref: str) -> SandboxRecord:
        try:
            return self._records[provider_ref]
        except KeyError as exc:
            raise ProviderError(self.name, f"unknown reference {provider_ref}") from exc

    def _now(self) -> datetime:
        return self._clock()

    def supports(self, currency: str, method: str) -> bool:
        return currency.upper() in self.supported_currencies and method.lower() in (
            self.supported_methods
        )

    # -- PaymentProvider -----------------------------------------------------------
    def authorize(
        self,
        *,
        payment_id: str,
        amount_minor: int,
        currency: str,
        method: str,
        auto_capture: bool,
        metadata: Mapping[str, Any],
    ) -> ProviderResult:
        if not self.supports(currency, method):
            raise ProviderError(self.name, f"{method}/{currency} not supported")
        ref = self._new_ref()
        outcome = str(metadata.get("sandbox_outcome", "")).lower()
        if outcome in {"fail", "decline", "declined"}:
            self._records[ref] = SandboxRecord(ref, amount_minor, currency, ProviderStatus.FAILED)
            return ProviderResult(
                status=ProviderStatus.FAILED,
                provider_ref=ref,
                message="Declined by sandbox (sandbox_outcome=fail)",
                raw={"payment_id": payment_id, "outcome": outcome},
            )
        record = SandboxRecord(ref, amount_minor, currency, ProviderStatus.AUTHORIZED)
        if auto_capture:
            record.status = ProviderStatus.CAPTURED
            record.captured_minor = amount_minor
            record.captured_on = self._now().date()
        self._records[ref] = record
        return ProviderResult(
            status=record.status,
            provider_ref=ref,
            redirect_url=self.redirect_url_for(ref),
            raw={"payment_id": payment_id, "method": method},
        )

    def capture(self, provider_ref: str, amount_minor: int) -> ProviderResult:
        record = self._get(provider_ref)
        if record.status is not ProviderStatus.AUTHORIZED:
            return ProviderResult(
                ProviderStatus.FAILED, provider_ref, message=f"cannot capture {record.status.value}"
            )
        if amount_minor > record.amount_minor:
            return ProviderResult(
                ProviderStatus.FAILED, provider_ref, message="capture exceeds authorisation"
            )
        record.status = ProviderStatus.CAPTURED
        record.captured_minor = amount_minor
        record.captured_on = self._now().date()
        return ProviderResult(ProviderStatus.CAPTURED, provider_ref)

    def refund(self, provider_ref: str, amount_minor: int, reason: str | None) -> ProviderResult:
        record = self._get(provider_ref)
        if record.status not in {ProviderStatus.CAPTURED, ProviderStatus.REFUNDED}:
            return ProviderResult(
                ProviderStatus.FAILED, provider_ref, message="nothing captured to refund"
            )
        if record.refunded_minor + amount_minor > record.captured_minor:
            return ProviderResult(
                ProviderStatus.FAILED, provider_ref, message="refund exceeds captured amount"
            )
        record.refunded_minor += amount_minor
        refund_ref = f"{provider_ref}-R{record.refunded_minor}"
        return ProviderResult(
            ProviderStatus.REFUNDED,
            refund_ref,
            raw={"reason": reason or "", "parent": provider_ref},
        )

    def cancel(self, provider_ref: str) -> ProviderResult:
        record = self._get(provider_ref)
        if record.status is not ProviderStatus.AUTHORIZED:
            return ProviderResult(
                ProviderStatus.FAILED, provider_ref, message=f"cannot cancel {record.status.value}"
            )
        record.status = ProviderStatus.CANCELLED
        return ProviderResult(ProviderStatus.CANCELLED, provider_ref)

    def fetch_report(self, settled_on: date) -> list[ProviderReportRow]:
        rows: list[ProviderReportRow] = []
        for record in self._records.values():
            if record.captured_on == settled_on and record.captured_minor:
                rows.append(
                    ProviderReportRow(
                        provider_ref=record.provider_ref,
                        amount_minor=record.captured_minor,
                        fee_minor=self.report_fee_rule.compute(record.captured_minor),
                        currency=record.currency,
                        settled_on=settled_on,
                    )
                )
        return rows

    # -- hooks subclasses customise ------------------------------------------------
    def redirect_url_for(self, provider_ref: str) -> str | None:
        return None

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def parse_webhook(self, body: bytes) -> ProviderWebhookEvent:  # pragma: no cover
        raise NotImplementedError

    # Test/ops helper used by the sandbox adapters to build a signed notification.
    def build_webhook(self, provider_ref: str, event: str) -> tuple[dict[str, str], bytes]:
        raise NotImplementedError

    @staticmethod
    def _load_json(body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Webhook body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("Webhook body must be a JSON object")
        return data
