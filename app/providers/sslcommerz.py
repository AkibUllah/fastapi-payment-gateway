"""SSLCommerz adapter (sandbox).

Field names follow the SSLCommerz IPN (tran_id, val_id, status). The sandbox signs with
a bare HMAC-SHA256 hex digest in `X-SSLCZ-Signature`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal

from app.core.money import FeeRule, decimal_to_minor, minor_to_decimal
from app.core.security import hmac_hex, verify_plain_hmac
from app.providers.base import ProviderWebhookEvent
from app.providers.sandbox import SandboxProvider

_STATUS_MAP = {
    "PENDING": "payment.authorized",
    "VALID": "payment.captured",
    "VALIDATED": "payment.captured",
    "FAILED": "payment.failed",
    "CANCELLED": "payment.cancelled",
    "REFUNDED": "payment.refunded",
}


class SSLCommerzAdapter(SandboxProvider):
    name = "sslcommerz"
    display_name = "SSLCommerz"
    supported_currencies = frozenset({"BDT"})
    supported_methods = frozenset({"card", "bkash", "nagad", "rocket", "netbanking"})
    ref_prefix = "SSLCZ"
    report_fee_rule = FeeRule(percent_bps=250, min_fee_minor=200)
    signature_header = "X-SSLCZ-Signature"

    def redirect_url_for(self, provider_ref: str) -> str | None:
        return f"https://sandbox.sslcommerz.com/EasyCheckOut/{provider_ref}"

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> None:
        lowered = {k.lower(): v for k, v in headers.items()}
        verify_plain_hmac(self.webhook_secret, body, lowered.get(self.signature_header.lower()))

    def parse_webhook(self, body: bytes) -> ProviderWebhookEvent:
        data = self._load_json(body)
        status = str(data.get("status", "")).upper()
        event_type = _STATUS_MAP.get(status)
        if event_type is None:
            raise ValueError(f"Unknown SSLCommerz status '{status}'")
        ref = data.get("tran_id")
        if not ref:
            raise ValueError("SSLCommerz IPN missing tran_id")
        currency = str(data.get("currency", "BDT"))
        amount = data.get("amount")
        amount_minor = decimal_to_minor(Decimal(str(amount)), currency) if amount else None
        return ProviderWebhookEvent(
            event_type=event_type,
            provider_ref=str(ref),
            amount_minor=amount_minor,
            currency=currency,
            raw=data,
        )

    def build_webhook(self, provider_ref: str, event: str) -> tuple[dict[str, str], bytes]:
        record = self._get(provider_ref)
        status = {v: k for k, v in _STATUS_MAP.items() if k != "VALIDATED"}[event]
        body = json.dumps(
            {
                "tran_id": provider_ref,
                "val_id": f"{provider_ref}VAL",
                "status": status,
                "amount": str(minor_to_decimal(record.amount_minor, record.currency)),
                "currency": record.currency,
                "store_id": "sandbox_store",
            }
        ).encode()
        return {self.signature_header: hmac_hex(self.webhook_secret, body)}, body
