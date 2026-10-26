"""Idempotency keys are reserved before any provider call, so duplicates can't double-charge."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.core.idempotency import (
    COMPLETED,
    IN_PROGRESS,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyStore,
    fingerprint,
)
from app.database import init_db
from app.models import IdempotencyRecord, Merchant
from app.providers.base import ProviderError

T0 = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
BODY = {"amount_minor": 150_000, "currency": "BDT", "method": "bkash"}


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _store(db, clock, lock_seconds=60) -> IdempotencyStore:
    return IdempotencyStore(db, lock_seconds=lock_seconds, clock=clock)


def _merchant_id(app) -> str:
    with app.state.session_factory() as db:
        return db.scalars(select(Merchant.id)).one()


# --------------------------------------------------------------------------- store level
def test_first_request_reserves_the_key_and_a_duplicate_is_told_to_wait(app, merchant):
    clock = Clock()
    mid = _merchant_id(app)
    with app.state.session_factory() as a, app.state.session_factory() as b:
        first = _store(a, clock).begin(mid, "k1", "h")
        assert first.record_id is not None and first.replay is None

        with pytest.raises(IdempotencyInProgressError) as err:
            _store(b, clock).begin(mid, "k1", "h")
        assert err.value.retry_after == 60

        clock.now = T0 + timedelta(seconds=45)
        with pytest.raises(IdempotencyInProgressError) as err:
            _store(b, clock).begin(mid, "k1", "h")
        assert err.value.retry_after == 15


def test_completed_reservation_replays_and_a_different_body_still_conflicts(app, merchant):
    clock = Clock()
    mid = _merchant_id(app)
    with app.state.session_factory() as db:
        store = _store(db, clock)
        reservation = store.begin(mid, "k1", "h")
        store.complete(reservation.record_id, 201, {"id": "pay_1"})
        db.commit()

        replay = store.begin(mid, "k1", "h").replay
        assert replay is not None and replay.status_code == 201
        with pytest.raises(IdempotencyConflictError):
            store.begin(mid, "k1", "different-body")


def test_body_mismatch_is_rejected_even_while_the_key_is_in_progress(app, merchant):
    clock = Clock()
    mid = _merchant_id(app)
    with app.state.session_factory() as db:
        _store(db, clock).begin(mid, "k1", "h")
        with pytest.raises(IdempotencyConflictError):
            _store(db, clock).begin(mid, "k1", "other")


def test_an_expired_reservation_is_taken_over_by_the_next_retry(app, merchant):
    clock = Clock()
    mid = _merchant_id(app)
    with app.state.session_factory() as db:
        first = _store(db, clock, lock_seconds=30).begin(mid, "k1", "h")
        clock.now = T0 + timedelta(seconds=31)  # holder crashed and never finished
        retry = _store(db, clock, lock_seconds=30).begin(mid, "k1", "h")
        assert retry.record_id == first.record_id
        row = db.get(IdempotencyRecord, retry.record_id)
        db.refresh(row)
        assert row.state == IN_PROGRESS
        assert row.locked_until.replace(tzinfo=timezone.utc) == clock.now + timedelta(seconds=30)


def test_losing_the_insert_race_reports_in_progress(app, merchant, monkeypatch):
    """Both requests saw no row; the second insert hits the unique constraint."""
    clock = Clock()
    mid = _merchant_id(app)
    with app.state.session_factory() as winner_db, app.state.session_factory() as loser_db:
        _store(winner_db, clock).begin(mid, "k1", "h")
        loser = _store(loser_db, clock)
        real_find = loser._find
        calls = {"n": 0}

        def stale_first_read(merchant_id, key):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_find(merchant_id, key)

        monkeypatch.setattr(loser, "_find", stale_first_read)
        with pytest.raises(IdempotencyInProgressError):
            loser.begin(mid, "k1", "h")
        assert calls["n"] == 2, "it must re-read after the IntegrityError"


def test_release_frees_the_key(app, merchant):
    clock = Clock()
    mid = _merchant_id(app)
    with app.state.session_factory() as db:
        store = _store(db, clock)
        store.release(store.begin(mid, "k1", "h").record_id)
        assert store.begin(mid, "k1", "h").record_id is not None


# --------------------------------------------------------------------------- API level
def _count_authorize(app, monkeypatch, fail_first: bool = False) -> dict[str, int]:
    """Wrap `authorize` on every registered provider; optionally fail the first call."""
    counter = {"calls": 0}
    for provider in app.state.providers.values():
        real = provider.authorize

        def wrapped(_real=real, **kwargs):
            counter["calls"] += 1
            if fail_first and counter["calls"] == 1:
                raise ProviderError("sandbox", "upstream timeout")
            return _real(**kwargs)

        monkeypatch.setattr(provider, "authorize", wrapped)
    return counter


def test_duplicate_arriving_mid_flight_gets_409_and_never_reaches_the_provider(
    app, client, auth, monkeypatch
):
    from app.schemas import PaymentCreate

    mid = _merchant_id(app)
    request_hash = fingerprint({"op": "create", **PaymentCreate(**BODY).model_dump(mode="json")})
    with app.state.session_factory() as db:
        # The first request has reserved the key and is still talking to the provider.
        IdempotencyStore(db, lock_seconds=60).begin(mid, "order-77", request_hash)
    counter = _count_authorize(app, monkeypatch)

    headers = {**auth, "Idempotency-Key": "order-77"}
    response = client.post("/v1/payments", json=BODY, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_in_progress"
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert counter["calls"] == 0, "the provider must not be called for the duplicate"
    assert client.get("/v1/payments", headers=auth).json() == []


def test_failed_request_releases_the_key_so_the_retry_succeeds(app, client, auth, monkeypatch):
    counter = _count_authorize(app, monkeypatch, fail_first=True)
    headers = {**auth, "Idempotency-Key": "order-88"}

    first = client.post("/v1/payments", json=BODY, headers=headers)
    assert first.status_code == 502

    with app.state.session_factory() as db:
        assert db.scalars(select(IdempotencyRecord)).all() == []

    retry = client.post("/v1/payments", json=BODY, headers=headers)
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "false"
    replay = client.post("/v1/payments", json=BODY, headers=headers)
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == retry.json()["id"]
    assert counter["calls"] == 2


def test_completed_record_is_stored_as_completed(app, client, auth):
    client.post("/v1/payments", json=BODY, headers={**auth, "Idempotency-Key": "order-99"})
    with app.state.session_factory() as db:
        row = db.scalars(select(IdempotencyRecord)).one()
        assert row.state == COMPLETED and row.locked_until is None and row.status_code == 201


def test_lock_seconds_setting_is_validated():
    assert Settings(_env_file=None).idempotency_lock_seconds == 60
    with pytest.raises(ValueError):
        Settings(_env_file=None, idempotency_lock_seconds=0)


# --------------------------------------------------------------------------- PostgreSQL
PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set TEST_DATABASE_URL to a PostgreSQL database")
def test_concurrent_requests_on_postgres_reserve_the_key_exactly_once():
    engine = create_engine(Settings(_env_file=None, database_url=PG_URL).database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    init_db(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        merchant = Merchant(
            name="Demo Shop",
            api_key_prefix="pfx",
            api_key_hash="x" * 64,
            webhook_url="https://merchant.example.com/hook",
            webhook_secret="s" * 24,
        )
        db.add(merchant)
        db.commit()
        mid = merchant.id

    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        with sessions() as db:
            barrier.wait()
            try:
                IdempotencyStore(db, lock_seconds=60).begin(mid, "same-key", "same-body")
                result = "reserved"
            except IdempotencyInProgressError:
                result = "in_progress"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.dispose()

    assert outcomes.count("reserved") == 1
    assert outcomes.count("in_progress") == workers - 1
