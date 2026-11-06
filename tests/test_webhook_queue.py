"""Claiming, leases and fencing on the webhook delivery queue."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Merchant, Payment, WebhookDelivery
from app.services.webhook_queue import DELIVERED, FAILED, PENDING, RETRYING, DeliveryQueue
from tests.conftest import T0

LEASE = 60


@pytest.fixture
def queue(app, clock) -> DeliveryQueue:
    return DeliveryQueue(app.state.session_factory, lease_seconds=LEASE, clock=clock)


@pytest.fixture
def add_delivery(app, merchant):
    with app.state.session_factory() as db:
        payment = Payment(
            merchant_id=merchant["id"],
            provider="mock",
            status="captured",
            amount_minor=1000,
            currency="BDT",
            method="bkash",
        )
        db.add(payment)
        db.commit()
        payment_id = payment.id

    def add(**overrides) -> str:
        values = {
            "merchant_id": merchant["id"],
            "payment_id": payment_id,
            "event_type": "payment.captured",
            "target_url": "https://merchant.example.com/hook",
            "payload": "{}",
            "next_attempt_at": T0,
            **overrides,
        }
        with app.state.session_factory() as db:
            delivery = WebhookDelivery(**values)
            db.add(delivery)
            db.commit()
            return delivery.id

    return add


def _row(app, delivery_id: str) -> WebhookDelivery:
    with app.state.session_factory() as db:
        return db.get(WebhookDelivery, delivery_id)


def test_claim_carries_what_a_send_needs_including_the_merchant_secret(queue, add_delivery, app):
    delivery_id = add_delivery()
    claim = queue.claim_due()
    assert claim.id == delivery_id
    with app.state.session_factory() as db:
        assert claim.webhook_secret == db.scalars(select(Merchant.webhook_secret)).one()


def test_rows_are_not_due_before_next_attempt_at(queue, clock, add_delivery):
    delivery_id = add_delivery(next_attempt_at=T0 + timedelta(seconds=30))
    assert queue.claim_due() is None
    clock.advance(30)
    assert queue.claim_due().id == delivery_id


def test_the_most_overdue_row_is_claimed_first(queue, add_delivery):
    add_delivery(next_attempt_at=T0)
    oldest = add_delivery(next_attempt_at=T0 - timedelta(hours=1))
    assert queue.claim_due().id == oldest


@pytest.mark.parametrize("status", [DELIVERED, FAILED])
def test_terminal_rows_are_never_claimed(queue, add_delivery, status):
    delivery_id = add_delivery(status=status)
    assert queue.claim_due() is None
    assert queue.claim_by_id(delivery_id) is None


def test_a_leased_row_cannot_be_claimed_twice(queue, add_delivery):
    delivery_id = add_delivery()
    assert queue.claim_due() is not None
    assert queue.claim_due() is None
    assert queue.claim_by_id(delivery_id) is None


def test_a_crashed_workers_row_comes_back_when_the_lease_expires(queue, clock, add_delivery):
    delivery_id = add_delivery()
    assert queue.claim_due() is not None
    clock.advance(LEASE - 1)
    assert queue.claim_due() is None
    clock.advance(1)
    assert queue.claim_due().id == delivery_id


def test_starting_an_attempt_counts_it_and_renews_the_lease(queue, clock, add_delivery, app):
    delivery_id = add_delivery()
    claim = queue.claim_due()
    clock.advance(50)
    assert queue.start_attempt(claim) is True
    assert _row(app, delivery_id).attempts == 1
    clock.advance(50)  # past the original lease, inside the renewed one
    assert queue.claim_due() is None


def test_a_worker_that_lost_its_lease_may_not_start_another_attempt(queue, clock, add_delivery):
    add_delivery()
    stale = queue.claim_due()
    clock.advance(LEASE)
    fresh = queue.claim_due()
    assert fresh.token != stale.token
    assert queue.start_attempt(stale) is False
    assert queue.start_attempt(fresh) is True


def test_a_stale_failure_cannot_overwrite_the_new_owner(queue, clock, add_delivery, app):
    delivery_id = add_delivery()
    stale = queue.claim_due()
    clock.advance(LEASE)
    fresh = queue.claim_due()

    recorded = queue.record_failure(
        stale, status_code=500, error="HTTP 500", retry_count=1, next_attempt_at=None
    )

    assert recorded is False
    row = _row(app, delivery_id)
    assert row.status == PENDING and row.claim_token == fresh.token and row.last_error is None


def test_a_late_success_wins_and_fences_out_the_new_owner(queue, clock, add_delivery, app):
    """The merchant acknowledged the event, so nothing should send it again."""
    delivery_id = add_delivery()
    stale = queue.claim_due()
    clock.advance(LEASE)
    fresh = queue.claim_due()

    queue.mark_delivered(stale, 200)

    assert queue.start_attempt(fresh) is False
    row = _row(app, delivery_id)
    assert row.status == DELIVERED and row.locked_until is None and row.next_attempt_at is None
    assert row.delivered_at is not None and row.response_status == 200


def test_record_failure_reschedules_and_releases_the_row(queue, clock, add_delivery, app):
    delivery_id = add_delivery()
    claim = queue.claim_due()
    retry_at = T0 + timedelta(seconds=300)

    assert queue.record_failure(
        claim, status_code=503, error="HTTP 503", retry_count=2, next_attempt_at=retry_at
    )

    row = _row(app, delivery_id)
    assert (row.status, row.retry_count, row.last_error) == (RETRYING, 2, "HTTP 503")
    assert row.locked_until is None and row.claim_token is None
    clock.now = retry_at
    assert queue.claim_due().id == delivery_id


def test_record_failure_without_a_next_attempt_is_final(queue, add_delivery, app):
    delivery_id = add_delivery()
    claim = queue.claim_due()
    queue.record_failure(
        claim, status_code=None, error="ConnectError", retry_count=5, next_attempt_at=None
    )
    row = _row(app, delivery_id)
    assert row.status == FAILED and row.next_attempt_at is None
    assert queue.claim_due() is None


def test_claim_by_id_ignores_the_grace_period_for_a_row_never_sent(queue, add_delivery):
    delivery_id = add_delivery(next_attempt_at=T0 + timedelta(seconds=60))
    assert queue.claim_by_id(delivery_id).id == delivery_id


def test_claim_by_id_leaves_a_rescheduled_row_to_its_schedule(queue, clock, add_delivery):
    """A slow background task must not re-send a row the dispatcher already tried."""
    delivery_id = add_delivery(
        status=RETRYING, attempts=1, next_attempt_at=T0 + timedelta(seconds=60)
    )
    assert queue.claim_by_id(delivery_id) is None
    clock.advance(60)
    assert queue.claim_by_id(delivery_id).id == delivery_id
