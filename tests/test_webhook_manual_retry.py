"""`POST /v1/webhooks/deliveries/{id}/retry`: merchant-scoped and idempotent."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.security import generate_api_key
from app.models import Merchant, WebhookDelivery
from app.services.webhook_queue import DeliveryQueue
from tests.conftest import create_payment


def _retry(client, auth, delivery_id: str):
    return client.post(f"/v1/webhooks/deliveries/{delivery_id}/retry", headers=auth)


def _delivery(client, auth) -> dict:
    return client.get("/v1/webhooks/deliveries", headers=auth).json()[0]


@pytest.fixture
def failed_delivery(app, client, auth, sink, clock) -> dict:
    """A delivery that has used up the whole schedule."""
    sink.fail_next = 1000
    create_payment(client, auth)
    for delay in (60, 300, 1800, 7200, 43200):
        clock.advance(delay)
        app.state.webhook_sender.dispatch_due(app.state.session_factory)
    sink.fail_next = 0
    sink.requests.clear()
    delivery = _delivery(client, auth)
    assert delivery["status"] == "failed"
    return delivery


def test_retry_redelivers_a_failed_event(client, auth, sink, failed_delivery):
    response = _retry(client, auth, failed_delivery["id"])

    assert response.status_code == 202
    assert len(sink.requests) == 1
    delivery = _delivery(client, auth)
    assert delivery["status"] == "delivered"
    assert delivery["attempts"] == failed_delivery["attempts"] + 1
    assert delivery["last_error"] is None


def test_retry_of_a_dead_endpoint_reopens_the_full_schedule(
    app, client, auth, sink, clock, failed_delivery
):
    sink.fail_next = 1000

    response = _retry(client, auth, failed_delivery["id"])

    assert response.status_code == 202
    assert len(sink.requests) == 1, "a manual retry is one attempt, not an in-request burst"
    with app.state.session_factory() as db:
        row = db.get(WebhookDelivery, failed_delivery["id"])
        assert (row.status, row.retry_count) == ("retrying", 0)

    for delay in (60, 300, 1800, 7200, 43200):
        clock.advance(delay)
        assert app.state.webhook_sender.dispatch_due(app.state.session_factory).claimed == 1
    assert _delivery(client, auth)["status"] == "failed"


def test_retry_of_a_delivered_event_changes_nothing(client, auth, sink):
    create_payment(client, auth)
    delivered = _delivery(client, auth)
    sink.requests.clear()

    response = _retry(client, auth, delivered["id"])

    assert response.status_code == 200
    assert response.json() == delivered
    assert sink.requests == []


def test_repeating_a_retry_after_success_does_not_send_again(client, auth, sink, failed_delivery):
    assert _retry(client, auth, failed_delivery["id"]).status_code == 202
    assert _retry(client, auth, failed_delivery["id"]).status_code == 200
    assert _retry(client, auth, failed_delivery["id"]).status_code == 200
    assert len(sink.requests) == 1


def test_retry_brings_a_waiting_row_forward_and_keeps_its_retries(app, client, auth, sink, clock):
    sink.fail_next = 1000
    create_payment(client, auth)
    clock.advance(60)
    app.state.webhook_sender.dispatch_due(app.state.session_factory)  # used one retry
    sink.fail_next = 1
    sink.requests.clear()

    delivery = _delivery(client, auth)
    assert _retry(client, auth, delivery["id"]).status_code == 202

    assert len(sink.requests) == 1
    with app.state.session_factory() as db:
        assert db.get(WebhookDelivery, delivery["id"]).retry_count == 1


def test_retry_collapses_into_a_send_already_in_flight(app, client, auth, sink, clock):
    sink.fail_next = 1000
    create_payment(client, auth)
    sink.fail_next = 0
    sink.requests.clear()
    delivery = _delivery(client, auth)
    clock.advance(60)
    queue = DeliveryQueue(app.state.session_factory, lease_seconds=60, clock=clock)
    assert queue.claim_due() is not None  # a worker is mid-send

    assert _retry(client, auth, delivery["id"]).status_code == 202

    assert sink.requests == []


def test_another_merchants_delivery_looks_missing(app, client, auth, sink, failed_delivery):
    plaintext, prefix, key_hash = generate_api_key("test")
    with app.state.session_factory() as db:
        db.add(
            Merchant(
                name="Other Shop",
                api_key_prefix=prefix,
                api_key_hash=key_hash,
                webhook_secret="s" * 24,
            )
        )
        db.commit()

    response = _retry(client, {"X-API-Key": plaintext}, failed_delivery["id"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert sink.requests == []
    with app.state.session_factory() as db:
        assert db.scalars(select(WebhookDelivery.status)).one() == "failed"


def test_unknown_delivery_is_404(client, auth):
    assert _retry(client, auth, "whk_missing").status_code == 404


def test_retry_needs_an_api_key(client, failed_delivery):
    assert _retry(client, {}, failed_delivery["id"]).status_code == 401


def test_delivery_log_shows_when_the_next_attempt_is(client, auth, sink):
    sink.fail_next = 3
    create_payment(client, auth)
    assert _delivery(client, auth)["next_attempt_at"].startswith("2026-11-06T10:01:00")
