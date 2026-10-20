"""MockProvider: a fully controllable adapter for tests and local demos."""

from __future__ import annotations

from collections.abc import Mapping

from app.core.security import hmac_hex, verify_plain_hmac
from app.providers.base import ProviderWebhookEvent
from app.providers.sandbox import SandboxProvider


class MockProvider(SandboxProvider):
    name = "mock"
    display_name = "Mock"
    supported_currencies = frozenset({"BDT", "USD"})
    supported_methods = frozenset({"card", "bkash", "mock"})
    ref_prefix = "MOCK"
    signature_header = "X-Mock-Signature"

    def __init__(self, webhook_secret: str = "mock-secret", **kwargs) -> None:
        super().__init__(webhook_secret, **kwargs)
        self.calls: list[tuple[str, str]] = []

    def authorize(self, **kwargs):  # type: ignore[override]
        self.calls.append(("authorize", kwargs["payment_id"]))
        return super().authorize(**kwargs)

    def capture(self, provider_ref: str, amount_minor: int):
        self.calls.append(("capture", provider_ref))
        return super().capture(provider_ref, amount_minor)

    def refund(self, provider_ref: str, amount_minor: int, reason: str | None):
        self.calls.append(("refund", provider_ref))
        return super().refund(provider_ref, amount_minor, reason)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> None:
        lowered = {k.lower(): v for k, v in headers.items()}
        verify_plain_hmac(self.webhook_secret, body, lowered.get(self.signature_header.lower()))

    def parse_webhook(self, body: bytes) -> ProviderWebhookEvent:
        data = self._load_json(body)
        return ProviderWebhookEvent(
            event_type=str(data["event"]),
            provider_ref=str(data["ref"]),
            amount_minor=data.get("amount_minor"),
            currency=data.get("currency"),
            raw=data,
        )

    def build_webhook(self, provider_ref: str, event: str) -> tuple[dict[str, str], bytes]:
        import json

        record = self._get(provider_ref)
        body = json.dumps(
            {
                "event": event,
                "ref": provider_ref,
                "amount_minor": record.amount_minor,
                "currency": record.currency,
            }
        ).encode()
        return {self.signature_header: hmac_hex(self.webhook_secret, body)}, body
