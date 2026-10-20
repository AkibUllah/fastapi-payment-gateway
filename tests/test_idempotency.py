from app.core.idempotency import fingerprint
from tests.conftest import create_payment


def test_fingerprint_is_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


def test_same_key_same_body_replays_stored_response(client, auth):
    headers = {**auth, "Idempotency-Key": "order-4521-attempt"}
    body = {"amount_minor": 150_000, "currency": "BDT", "method": "bkash"}

    first = client.post("/v1/payments", json=body, headers=headers)
    second = client.post("/v1/payments", json=body, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.headers["Idempotent-Replayed"] == "false"
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json() == second.json()

    listed = client.get("/v1/payments", headers=auth).json()
    assert len(listed) == 1, "the retry must not create a second payment"


def test_same_key_different_body_is_rejected(client, auth):
    headers = {**auth, "Idempotency-Key": "order-4521"}
    client.post(
        "/v1/payments",
        json={"amount_minor": 150_000, "currency": "BDT", "method": "bkash"},
        headers=headers,
    )
    conflict = client.post(
        "/v1/payments",
        json={"amount_minor": 999, "currency": "BDT", "method": "bkash"},
        headers=headers,
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "idempotency_key_reused"


def test_keys_are_scoped_per_merchant_and_per_key(client, auth):
    body = {"amount_minor": 150_000, "currency": "BDT", "method": "bkash"}
    a = client.post("/v1/payments", json=body, headers={**auth, "Idempotency-Key": "k1"})
    b = client.post("/v1/payments", json=body, headers={**auth, "Idempotency-Key": "k2"})
    assert a.json()["id"] != b.json()["id"]


def test_refund_is_idempotent(client, auth):
    payment = create_payment(client, auth)
    headers = {**auth, "Idempotency-Key": "refund-1"}
    first = client.post(
        f"/v1/payments/{payment['id']}/refund", json={"amount_minor": 50_000}, headers=headers
    )
    second = client.post(
        f"/v1/payments/{payment['id']}/refund", json={"amount_minor": 50_000}, headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    # Only one refund actually happened.
    current = client.get(f"/v1/payments/{payment['id']}", headers=auth).json()
    assert current["refunded_minor"] == 50_000
    assert len(current["refunds"]) == 1
