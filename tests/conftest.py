from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.routing import PaymentRouter
from app.core.security import generate_api_key
from app.main import create_app
from app.models import Merchant
from app.providers.mock import MockProvider
from app.services.webhooks import WebhookSender

MERCHANT_WEBHOOK_URL = "https://merchant.example.com/payments/webhook"


@dataclass
class WebhookSink:
    """Captures outgoing merchant webhooks; can be told to fail the next N requests."""

    requests: list[httpx.Request] = field(default_factory=list)
    fail_next: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, text="try later")
        return httpx.Response(200, json={"received": True})

    @property
    def payloads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        webhook_retry_backoff_seconds=0,
        log_level="WARNING",
    )


@pytest.fixture
def sink() -> WebhookSink:
    return WebhookSink()


@pytest.fixture
def app(settings: Settings, sink: WebhookSink):
    application = create_app(settings)
    application.state.providers["mock"] = MockProvider()
    application.state.router = PaymentRouter(application.state.providers)
    application.state.webhook_sender = WebhookSender(
        timeout=1.0,
        max_attempts=3,
        backoff_seconds=0,
        transport=httpx.MockTransport(sink.handler),
        sleep=lambda _seconds: None,
    )
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def merchant(app, client) -> dict[str, str]:
    plaintext, prefix, key_hash = generate_api_key("test")
    with app.state.session_factory() as db:
        record = Merchant(
            name="Dhaka Mart",
            api_key_prefix=prefix,
            api_key_hash=key_hash,
            webhook_url=MERCHANT_WEBHOOK_URL,
            webhook_secret="whsec_test_merchant_secret",
        )
        db.add(record)
        db.commit()
        return {
            "id": record.id,
            "api_key": plaintext,
            "webhook_secret": record.webhook_secret,
        }


@pytest.fixture
def auth(merchant) -> dict[str, str]:
    return {"X-API-Key": merchant["api_key"]}


@pytest.fixture
def db_session(app):
    with app.state.session_factory() as session:
        yield session


def create_payment(client, auth, **overrides) -> dict:
    body = {
        "amount_minor": 150_000,
        "currency": "BDT",
        "method": "bkash",
        "capture": "automatic",
        "customer_ref": "cust_01711000000",
        "description": "Order #4521",
    }
    body.update(overrides)
    response = client.post("/v1/payments", json=body, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()
