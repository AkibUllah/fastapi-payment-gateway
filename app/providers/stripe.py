"""Stripe adapter (sandbox).

Uses Stripe's `Stripe-Signature: t=...,v1=...` scheme, which this gateway also uses for
its own outgoing webhooks. Amounts in Stripe events are already in minor units.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping

from app.core.money import FeeRule
from app.core.security import build_signature_header, verify_signature_header
from app.providers.base import ProviderWebhookEvent
from app.providers.sandbox import SandboxProvider

_EVENT_MAP = {
    "payment_intent.amount_capturable_updated": "payment.authorized",
    "payment_intent.succeeded": "payment.captured",
    "payment_intent.payment_failed": "payment.failed",
    "payment_intent.canceled": "payment.cancelled",
    "charge.refunded": "payment.refunded",
}


class StripeAdapter(SandboxProvider):
    name = "stripe"
    display_name = "Stripe"
    supported_currencies = frozenset({"USD", "EUR", "GBP"})
    supported_methods = frozenset({"card"})
    ref_prefix = "pi_"
    report_fee_rule = FeeRule(percent_bps=290, fixed_minor=30)
    signature_header = "Stripe-Signature"

    def __init__(self, webhook_secret: str, tolerance_seconds: int = 300, **kwargs) -> None:
        super().__init__(webhook_secret, **kwargs)
        self.tolerance_seconds = tolerance_seconds

    def _new_ref(self) -> str:
        return f"pi_{secrets.token_hex(8)}"

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> None:
        lowered = {k.lower(): v for k, v in headers.items()}
        verify_signature_header(
            self.webhook_secret,
            body,
            lowered.get(self.signature_header.lower()),
            tolerance_seconds=self.tolerance_seconds,
        )

    def parse_webhook(self, body: bytes) -> ProviderWebhookEvent:
        data = self._load_json(body)
        event_type = _EVENT_MAP.get(str(data.get("type", "")))
        if event_type is None:
            raise ValueError(f"Unhandled Stripe event type '{data.get('type')}'")
        obj = data.get("data", {}).get("object", {})
        ref = obj.get("payment_intent") or obj.get("id")
        if not ref:
            raise ValueError("Stripe event missing object id")
        amount = obj.get("amount_received") or obj.get("amount")
        return ProviderWebhookEvent(
            event_type=event_type,
            provider_ref=str(ref),
            amount_minor=int(amount) if amount is not None else None,
            currency=str(obj.get("currency", "usd")).upper(),
            raw=data,
        )

    def build_webhook(self, provider_ref: str, event: str) -> tuple[dict[str, str], bytes]:
        record = self._get(provider_ref)
        stripe_type = {v: k for k, v in _EVENT_MAP.items()}[event]
        body = json.dumps(
            {
                "id": f"evt_{provider_ref[3:]}",
                "type": stripe_type,
                "data": {
                    "object": {
                        "id": provider_ref,
                        "object": "payment_intent",
                        "amount": record.amount_minor,
                        "amount_received": record.captured_minor or record.amount_minor,
                        "currency": record.currency.lower(),
                    }
                },
            }
        ).encode()
        return {self.signature_header: build_signature_header(self.webhook_secret, body)}, body
