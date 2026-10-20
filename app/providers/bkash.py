"""bKash Tokenized Checkout adapter (sandbox).

Field names mirror the bKash API (paymentID, trxID, transactionStatus). The sandbox
signs notifications with a bare HMAC-SHA256 hex digest in `X-Bkash-Signature`.
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
    "Initiated": "payment.authorized",
    "Completed": "payment.captured",
    "Failed": "payment.failed",
    "Cancelled": "payment.cancelled",
    "Refunded": "payment.refunded",
}


class BkashAdapter(SandboxProvider):
    name = "bkash"
    display_name = "bKash"
    supported_currencies = frozenset({"BDT"})
    supported_methods = frozenset({"bkash"})
    ref_prefix = "TRX"
    report_fee_rule = FeeRule(percent_bps=185)
    signature_header = "X-Bkash-Signature"

    def redirect_url_for(self, provider_ref: str) -> str | None:
        return (
            f"https://sandbox.payment.bkash.com/redirect/tokenized/checkout/execute/{provider_ref}"
        )

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> None:
        verify_plain_hmac(self.webhook_secret, body, _header(headers, self.signature_header))

    def parse_webhook(self, body: bytes) -> ProviderWebhookEvent:
        data = self._load_json(body)
        status = str(data.get("transactionStatus", ""))
        event_type = _STATUS_MAP.get(status)
        if event_type is None:
            raise ValueError(f"Unknown bKash transactionStatus '{status}'")
        ref = data.get("trxID") or data.get("paymentID")
        if not ref:
            raise ValueError("bKash notification missing trxID/paymentID")
        amount = data.get("amount")
        currency = str(data.get("currency", "BDT"))
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
        status = {v: k for k, v in _STATUS_MAP.items()}[event]
        body = json.dumps(
            {
                "paymentID": f"TR00{provider_ref[-6:]}",
                "trxID": provider_ref,
                "transactionStatus": status,
                "amount": str(minor_to_decimal(record.amount_minor, record.currency)),
                "currency": record.currency,
                "intent": "sale",
            }
        ).encode()
        return {self.signature_header: hmac_hex(self.webhook_secret, body)}, body


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())
