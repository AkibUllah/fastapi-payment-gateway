"""Concurrency guarantees that only a real PostgreSQL can prove (SQLite has no row locks)."""

from __future__ import annotations

import os
import threading
import time
from collections import Counter

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import init_db
from app.models import Merchant, Payment, WebhookDelivery, utc_now
from app.services.webhook_queue import DELIVERED, DeliveryQueue
from app.services.webhooks import DELIVERY_HEADER, WebhookSender

PG_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set TEST_DATABASE_URL to PostgreSQL")


def _engine() -> Engine:
    return create_engine(Settings(_env_file=None, database_url=PG_URL).database_url)


@pytest.fixture
def engine():
    engine = _engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(sessions, count: int) -> list[str]:
    with sessions() as db:
        merchant = Merchant(
            name="Demo Shop",
            api_key_prefix="pfx",
            api_key_hash="x" * 64,
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
        rows = [
            WebhookDelivery(
                merchant_id=merchant.id,
                payment_id=payment.id,
                event_type="payment.captured",
                target_url=merchant.webhook_url,
                payload="{}",
                next_attempt_at=utc_now(),
            )
            for _ in range(count)
        ]
        db.add_all(rows)
        db.commit()
        return [row.id for row in rows]


def test_concurrent_workers_send_every_row_exactly_once(sessions):
    ids = _seed(sessions, 40)
    sent: Counter[str] = Counter()
    lock = threading.Lock()

    def merchant_endpoint(request: httpx.Request) -> httpx.Response:
        time.sleep(0.005)  # long enough for the workers to overlap on the queue
        with lock:
            sent[request.headers[DELIVERY_HEADER]] += 1
        return httpx.Response(200)

    workers = 4
    barrier = threading.Barrier(workers)
    claimed: list[int] = []

    def worker() -> None:
        # One engine per worker, like separate processes.
        worker_engine = _engine()
        factory = sessionmaker(bind=worker_engine, expire_on_commit=False)
        sender = WebhookSender(backoff_seconds=0, transport=httpx.MockTransport(merchant_endpoint))
        barrier.wait()
        stats = sender.dispatch_due(factory, limit=len(ids))
        worker_engine.dispose()
        with lock:
            claimed.append(stats.claimed)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sent == Counter(ids), "every row sent once, none twice"
    assert sum(claimed) == len(ids)
    assert len([c for c in claimed if c]) > 1, "the work was actually shared"
    with sessions() as db:
        assert set(db.scalars(select(WebhookDelivery.status))) == {DELIVERED}


def test_a_claim_skips_a_row_locked_by_another_transaction(sessions):
    locked_id, free_id = _seed(sessions, 2)
    queue = DeliveryQueue(sessions)
    with sessions() as holder:
        holder.execute(
            select(WebhookDelivery).where(WebhookDelivery.id == locked_id).with_for_update()
        )
        started = time.monotonic()
        claim = queue.claim_due()
        assert claim.id == free_id
        assert queue.claim_due() is None, "skips the locked row instead of waiting on it"
        assert time.monotonic() - started < 2


def test_no_lock_is_held_while_the_merchant_endpoint_responds(sessions):
    """A slow merchant must not pin a row lock, or the retry endpoint would hang behind it."""
    [delivery_id] = _seed(sessions, 1)
    observed: list[str] = []

    def merchant_endpoint(_request: httpx.Request) -> httpx.Response:
        with sessions() as db:
            db.execute(text("SET LOCAL lock_timeout = '500ms'"))
            try:
                db.execute(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.id == delivery_id)
                    .with_for_update()
                )
                observed.append("free")
            except OperationalError:
                observed.append("locked")
            db.rollback()
        return httpx.Response(200)

    sender = WebhookSender(transport=httpx.MockTransport(merchant_endpoint))
    assert sender.dispatch_due(sessions).delivered == 1
    assert observed == ["free"]


def test_lease_comparison_holds_in_a_non_utc_session(sessions):
    """A worker's time zone must not shift which rows look due or leased."""
    [delivery_id] = _seed(sessions, 1)
    dhaka = create_engine(
        Settings(_env_file=None, database_url=PG_URL).database_url,
        connect_args={"options": "-c timezone=Asia/Dhaka"},
    )
    queue = DeliveryQueue(sessionmaker(bind=dhaka, expire_on_commit=False))

    claim = queue.claim_due()

    assert claim is not None and claim.id == delivery_id
    assert queue.claim_due() is None
    dhaka.dispose()
