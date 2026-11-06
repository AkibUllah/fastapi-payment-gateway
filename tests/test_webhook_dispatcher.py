"""The dispatcher walks the retry schedule, survives restarts and never double-sends."""

from __future__ import annotations

import json
import threading
from datetime import timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.security import generate_api_key, verify_signature_header
from app.database import init_db, make_engine, make_session_factory
from app.models import Merchant, Payment, WebhookDelivery
from app.services.webhook_queue import DELIVERED, FAILED, RETRYING, DeliveryQueue
from app.services.webhooks import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    WebhookOutbox,
    WebhookSender,
)
from tests.conftest import Clock, create_payment

SCHEDULE = (60, 300, 1800, 7200, 43200)


def _only_row(app) -> WebhookDelivery:
    with app.state.session_factory() as db:
        return db.scalars(select(WebhookDelivery)).one()


def _dispatch(app):
    return app.state.webhook_sender.dispatch_due(app.state.session_factory)


def _fail_in_request(client, auth, sink, failures: int = 1000) -> None:
    sink.fail_next = failures
    create_payment(client, auth)


def test_nothing_is_sent_before_the_retry_is_due(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink)
    sent = len(sink.requests)

    clock.advance(59)
    stats = _dispatch(app)

    assert stats.claimed == 0 and len(sink.requests) == sent


def test_due_retry_succeeds_when_the_merchant_recovers(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink, failures=3)

    clock.advance(60)
    stats = _dispatch(app)

    assert (stats.claimed, stats.delivered) == (1, 1)
    row = _only_row(app)
    assert (row.status, row.attempts, row.last_error) == (DELIVERED, 4, None)
    assert row.next_attempt_at is None


def test_each_failure_moves_one_step_down_the_schedule(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink)

    for step, delay in enumerate(SCHEDULE[:-1], start=1):
        clock.advance(delay)
        assert _dispatch(app).rescheduled == 1
        row = _only_row(app)
        assert (row.status, row.retry_count) == (RETRYING, step)
        expected = clock.now + timedelta(seconds=SCHEDULE[step])
        assert row.next_attempt_at.replace(tzinfo=timezone.utc) == expected


def test_failed_is_final_only_after_the_whole_schedule(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink)

    for delay in SCHEDULE:
        clock.advance(delay)
        _dispatch(app)

    row = _only_row(app)
    assert (row.status, row.next_attempt_at) == (FAILED, None)
    assert (row.attempts, row.retry_count) == (3 + len(SCHEDULE), len(SCHEDULE))
    assert len(sink.requests) == 3 + len(SCHEDULE)

    clock.advance(10 * 86400)
    assert _dispatch(app).claimed == 0


def test_one_pass_makes_a_single_attempt_per_row(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink)
    sent = len(sink.requests)

    clock.advance(60)
    stats = _dispatch(app)

    assert stats.claimed == 1 and len(sink.requests) == sent + 1


def test_limit_caps_a_pass(app, client, auth, sink, clock):
    sink.fail_next = 1000
    for _ in range(3):
        create_payment(client, auth)
    sink.fail_next = 0
    clock.advance(60)

    assert app.state.webhook_sender.dispatch_due(app.state.session_factory, limit=2).claimed == 2
    assert _dispatch(app).claimed == 1


def test_retries_carry_the_same_id_and_a_fresh_signature(
    app, client, auth, sink, clock, merchant, monkeypatch
):
    """Hours later the first signature would be stale; the delivery id must not change."""
    _fail_in_request(client, auth, sink, failures=3)
    clock.advance(60)
    monkeypatch.setattr("app.core.security.time.time", lambda: 2_000_000_000)
    _dispatch(app)

    first, retry = sink.requests[0], sink.requests[-1]
    assert retry.headers[DELIVERY_HEADER] == first.headers[DELIVERY_HEADER]
    assert retry.content == first.content
    assert retry.headers[SIGNATURE_HEADER] != first.headers[SIGNATURE_HEADER]
    verify_signature_header(
        merchant["webhook_secret"],
        retry.content,
        retry.headers[SIGNATURE_HEADER],
        now=2_000_000_000,
    )


def test_a_poison_row_does_not_stop_the_rest_of_the_pass(app, client, auth, sink, clock):
    sink.fail_next = 1000
    create_payment(client, auth)
    create_payment(client, auth)
    sink.fail_next = 0
    with app.state.session_factory() as db:
        first = db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).first()
        first.target_url = "https://exa mple.com:bad/"
        poison_id = first.id
        db.commit()
    clock.advance(60)

    stats = _dispatch(app)

    assert (stats.rescheduled, stats.delivered) == (1, 1)
    with app.state.session_factory() as db:
        assert db.get(WebhookDelivery, poison_id).status == RETRYING


def test_worker_crash_mid_send_resends_once_after_the_lease(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink, failures=3)
    clock.advance(60)
    queue = DeliveryQueue(app.state.session_factory, lease_seconds=60, clock=clock)
    claim = queue.claim_due()  # this worker is killed here, holding the lease
    assert queue.start_attempt(claim)
    sent = len(sink.requests)

    assert _dispatch(app).claimed == 0, "the lease still protects the row"
    clock.advance(60)
    assert _dispatch(app).delivered == 1

    assert len(sink.requests) == sent + 1
    assert _only_row(app).status == DELIVERED


def test_lease_lost_during_a_slow_send_is_reported(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink, failures=3)
    clock.advance(60)
    sender = app.state.webhook_sender
    factory = app.state.session_factory

    def overrun_then_refuse(request: httpx.Request) -> httpx.Response:
        clock.advance(120)
        DeliveryQueue(factory, lease_seconds=60, clock=clock).claim_due()
        return httpx.Response(500)

    sender.transport = httpx.MockTransport(overrun_then_refuse)

    assert sender.dispatch_due(factory, limit=1).lost == 1


def test_state_survives_a_full_restart(tmp_path):
    """Two independent engines over one file: nothing lives in the first process."""
    url = f"sqlite:///{tmp_path / 'gateway.db'}"
    clock = Clock()

    def boot(handler):
        engine = make_engine(url)
        init_db(engine)
        sender = WebhookSender(
            max_attempts=1,
            backoff_seconds=0,
            transport=httpx.MockTransport(handler),
            clock=clock,
            rng=lambda: 0.5,
        )
        return engine, make_session_factory(engine), sender

    engine, factory, sender = boot(lambda _r: httpx.Response(503))
    _, prefix, key_hash = generate_api_key("test")
    with factory() as db:
        merchant = Merchant(
            name="Demo Shop",
            api_key_prefix=prefix,
            api_key_hash=key_hash,
            webhook_url="https://merchant.example.com/hook",
            webhook_secret="s" * 24,
        )
        db.add(merchant)
        db.flush()
        payment = Payment(
            merchant_id=merchant.id,
            provider="mock",
            status="captured",
            amount_minor=1000,
            currency="BDT",
            method="bkash",
        )
        db.add(payment)
        db.flush()
        outbox = WebhookOutbox(db, clock=clock)
        delivery_id = outbox.enqueue(merchant, payment, "payment.captured").id
        db.commit()
    sender.deliver(factory, [delivery_id])
    engine.dispose()

    received: list[httpx.Request] = []

    def healthy(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    engine, factory, sender = boot(healthy)
    clock.advance(60)
    assert sender.dispatch_due(factory).delivered == 1
    assert json.loads(received[0].content)["id"] == delivery_id
    with factory() as db:
        assert db.get(WebhookDelivery, delivery_id).status == DELIVERED
    engine.dispose()


def test_a_second_worker_skips_the_row_in_flight(app, client, auth, sink, clock):
    sink.fail_next = 1000
    ids = {create_payment(client, auth)["id"] for _ in range(6)}
    assert len(ids) == 6
    sink.fail_next = 0
    sink.requests.clear()
    clock.advance(60)

    sender = app.state.webhook_sender
    # Worker A is suspended inside its first send while worker B drains the queue.
    real_handler = sink.handler
    nested = {"done": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not nested["done"]:
            nested["done"] = True
            sender.dispatch_due(app.state.session_factory)
        return real_handler(request)

    sender.transport = httpx.MockTransport(handler)
    sender.dispatch_due(app.state.session_factory)

    delivered = [r.headers[DELIVERY_HEADER] for r in sink.requests]
    assert len(delivered) == 6 and len(set(delivered)) == 6


def test_run_forever_exits_when_stopped(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink, failures=3)
    clock.advance(60)
    sender = app.state.webhook_sender
    stop = threading.Event()
    waits: list[float] = []
    real_wait = stop.wait

    def wait_then_stop(seconds: float) -> bool:
        waits.append(seconds)
        stop.set()
        return real_wait(0)

    stop.wait = wait_then_stop

    sender.run_forever(app.state.session_factory, stop, poll_seconds=7, batch_size=10)

    assert waits == [7]
    assert _only_row(app).status == DELIVERED


def test_run_forever_rides_out_a_database_outage(app, client, auth, sink, clock, monkeypatch):
    _fail_in_request(client, auth, sink, failures=3)
    clock.advance(60)
    sender = app.state.webhook_sender
    real_dispatch = sender.dispatch_due
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("select 1", {}, Exception("server closed the connection"))
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(sender, "dispatch_due", flaky)
    stop = threading.Event()
    real_wait = stop.wait

    def wait(seconds: float) -> bool:
        if calls["n"] >= 2:
            stop.set()
        return real_wait(0)

    stop.wait = wait

    sender.run_forever(app.state.session_factory, stop)

    assert calls["n"] == 2
    assert _only_row(app).status == DELIVERED


def test_a_stop_request_ends_the_pass_between_rows(app, client, auth, sink, clock):
    _fail_in_request(client, auth, sink, failures=3)
    clock.advance(60)
    stop = threading.Event()
    stop.set()

    stats = app.state.webhook_sender.dispatch_due(app.state.session_factory, stop=stop)

    assert stats.claimed == 0
