"""Build the provider map from settings. Registration order is the routing tie-breaker."""

from __future__ import annotations

from app.config import Settings
from app.providers.base import PaymentProvider
from app.providers.bkash import BkashAdapter
from app.providers.sslcommerz import SSLCommerzAdapter
from app.providers.stripe import StripeAdapter


def build_providers(settings: Settings) -> dict[str, PaymentProvider]:
    providers: list[PaymentProvider] = [
        BkashAdapter(settings.bkash_webhook_secret),
        SSLCommerzAdapter(settings.sslcommerz_webhook_secret),
        StripeAdapter(
            settings.stripe_webhook_secret,
            tolerance_seconds=settings.webhook_signature_tolerance_seconds,
        ),
    ]
    return {provider.name: provider for provider in providers}
