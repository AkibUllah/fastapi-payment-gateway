"""Failed in-request deliveries are rescheduled durably instead of being lost."""

from __future__ import annotations

from datetime import timedelta, timezone

import httpx
from sqlalchemy import select

from app.models import Merchant, WebhookDelivery
from app.services.webhook_queue import DELIVERED, FAILED, PENDING, RETRYING, DeliveryQueue
from app.services.webhooks import WebhookSender
from tests.conftest import T0, create_payment


def _rows(app) -> list[WebhookDelivery]:
    with app.state.session_factory() as db:
        return list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.created_at)))


def test_new_delivery_is_durable_before_anything_is_sent(app, auth, client, monkeypatch):
    """If the process dies before the background task, the row is already due for a worker."""
    monkeypatch.setattr(app.state.webhook_sender, "deliver", lambda *_args: None)

    create_payment(client, auth)

    row = _rows(app)[0]
    assert (row.status, row.attempts) == (PENDING, 0)
    assert row.next_attempt_at.replace(tzinfo=timezone.utc) == T0 + timedelta(seconds=60)


def test_failed_request_attempts_schedule_the_first_retry(app, auth, client, sink):
    sink.fail_next = 3

    create_payment(client, auth)

    row = _rows(app)[0]
    assert (row.status, row.attempts, row.retry_count) == (RETRYING, 3, 0)
    assert row.next_attempt_at.replace(tzinfo=timezone.utc) == T0 + timedelta(seconds=60)
    assert row.locked_until is None and row.claim_token is None
    assert row.last_error == "HTTP 503" and row.response_status == 503


def test_retry_time_is_jittered(app, auth, client, sink):
    app.state.webhook_sender = WebhookSender(
        max_attempts=1,
        backoff_seconds=0,
        transport=httpx.MockTransport(sink.handler),
        clock=app.state.webhook_sender.clock,
        jitter=0.1,
        rng=lambda: 1.0,
    )
    sink.fail_next = 1

    create_payment(client, auth)

    assert _rows(app)[0].next_attempt_at.replace(tzinfo=timezone.utc) == T0 + timedelta(seconds=66)


def test_network_errors_are_rescheduled_too(app, auth, client):
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    app.state.webhook_sender.transport = httpx.MockTransport(refuse)

    create_payment(client, auth)

    row = _rows(app)[0]
    assert (row.status, row.response_status) == (RETRYING, None)
    assert row.last_error.startswith("ConnectError")


def test_success_clears_the_schedule_and_the_lease(app, auth, client):
    create_payment(client, auth)
    row = _rows(app)[0]
    assert row.status == DELIVERED
    assert row.next_attempt_at is None and row.locked_until is None and row.claim_token is None


def test_an_invalid_target_url_fails_the_attempt_instead_of_raising(app, auth, client, merchant):
    with app.state.session_factory() as db:
        db.get(Merchant, merchant["id"]).webhook_url = "https://exa mple.com:bad/"
        db.commit()
    app.state.webhook_sender.transport = None

    create_payment(client, auth)

    row = _rows(app)[0]
    assert row.status == RETRYING and row.last_error.startswith("InvalidURL")


def test_empty_schedule_keeps_the_old_give_up_after_the_request_behaviour(
    app, auth, client, sink, clock
):
    app.state.webhook_sender.retry_schedule = ()
    sink.fail_next = 3

    create_payment(client, auth)

    assert _rows(app)[0].status == FAILED


def test_in_request_send_skips_a_row_a_worker_already_owns(app, auth, client, sink, clock):
    """Background task vs dispatcher: the claim decides, and only one of them sends."""
    sender = app.state.webhook_sender
    real_deliver = sender.deliver
    captured: list[list[str]] = []
    sender.deliver = lambda _factory, ids: captured.append(list(ids))
    create_payment(client, auth)
    assert sink.requests == []

    queue = DeliveryQueue(app.state.session_factory, lease_seconds=60, clock=clock)
    assert queue.claim_by_id(captured[0][0]) is not None

    real_deliver(app.state.session_factory, captured[0])

    assert sink.requests == []
