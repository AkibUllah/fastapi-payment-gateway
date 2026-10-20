from tests.conftest import create_payment


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert {"bkash", "sslcommerz", "stripe"} <= set(body["providers"])
    assert "X-Request-ID" in response.headers


def test_requests_without_api_key_are_rejected(client):
    assert client.get("/v1/payments").status_code == 401
    assert (
        client.get("/v1/payments", headers={"X-API-Key": "pk_test_bogus0000000"}).status_code == 401
    )
    body = client.get("/v1/payments", headers={"X-API-Key": "nonsense"}).json()
    assert body["error"]["code"] == "unauthorized"


def test_create_auto_capture_bkash_payment(client, auth):
    payment = create_payment(client, auth)
    assert payment["status"] == "captured"
    assert payment["provider"] == "bkash"
    assert payment["provider_ref"].startswith("TRX")
    assert payment["amount"] == "৳1,500.00"
    assert payment["fee_minor"] == 2_775  # 1.85 %
    assert payment["net_minor"] == 150_000 - 2_775
    assert payment["redirect_url"].startswith("https://sandbox.payment.bkash.com/")
    assert [e["to_status"] for e in payment["events"]] == ["created", "captured"]


def test_manual_capture_flow(client, auth):
    payment = create_payment(client, auth, method="card", capture="manual")
    assert payment["status"] == "authorized"
    assert payment["provider"] == "sslcommerz"

    captured = client.post(f"/v1/payments/{payment['id']}/capture", headers=auth)
    assert captured.status_code == 200
    assert captured.json()["status"] == "captured"
    assert captured.json()["captured_at"] is not None

    again = client.post(f"/v1/payments/{payment['id']}/capture", headers=auth)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"


def test_partial_capture_requotes_fee(client, auth):
    payment = create_payment(client, auth, method="card", capture="manual", amount_minor=100_000)
    response = client.post(
        f"/v1/payments/{payment['id']}/capture", json={"amount_minor": 40_000}, headers=auth
    )
    assert response.status_code == 200
    body = response.json()
    assert body["amount_minor"] == 40_000
    assert body["fee_minor"] == 1_000  # 2.5 % of 400.00 BDT

    too_much = create_payment(client, auth, method="card", capture="manual", amount_minor=1_000)
    over = client.post(
        f"/v1/payments/{too_much['id']}/capture", json={"amount_minor": 5_000}, headers=auth
    )
    assert over.status_code == 422


def test_cancel_authorized_payment_and_reject_cancel_after_capture(client, auth):
    authorized = create_payment(client, auth, capture="manual")
    cancelled = client.post(f"/v1/payments/{authorized['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    captured = create_payment(client, auth)
    denied = client.post(f"/v1/payments/{captured['id']}/cancel", headers=auth)
    assert denied.status_code == 409


def test_refund_math_partial_then_full(client, auth):
    payment = create_payment(client, auth, amount_minor=100_000)

    first = client.post(
        f"/v1/payments/{payment['id']}/refund",
        json={"amount_minor": 30_000, "reason": "one item returned"},
        headers=auth,
    )
    assert first.status_code == 200
    body = first.json()
    assert body["refund"]["amount_minor"] == 30_000
    assert body["payment"]["status"] == "partially_refunded"
    assert body["payment"]["refunded_minor"] == 30_000
    assert body["payment"]["refundable_minor"] == 70_000

    second = client.post(f"/v1/payments/{payment['id']}/refund", headers=auth)  # remainder
    assert second.status_code == 200
    assert second.json()["refund"]["amount_minor"] == 70_000
    assert second.json()["payment"]["status"] == "refunded"
    assert second.json()["payment"]["refundable_minor"] == 0

    third = client.post(f"/v1/payments/{payment['id']}/refund", headers=auth)
    assert third.status_code == 409


def test_refund_exceeding_balance_is_rejected(client, auth):
    payment = create_payment(client, auth, amount_minor=10_000)
    response = client.post(
        f"/v1/payments/{payment['id']}/refund", json={"amount_minor": 10_001}, headers=auth
    )
    assert response.status_code == 422
    assert "exceeds" in response.json()["error"]["message"]


def test_refund_before_capture_is_conflict(client, auth):
    payment = create_payment(client, auth, capture="manual")
    response = client.post(f"/v1/payments/{payment['id']}/refund", headers=auth)
    assert response.status_code == 409


def test_sandbox_decline_produces_failed_payment(client, auth):
    payment = create_payment(client, auth, metadata={"sandbox_outcome": "fail"})
    assert payment["status"] == "failed"
    assert "Declined" in payment["failure_reason"]
    assert client.post(f"/v1/payments/{payment['id']}/capture", headers=auth).status_code == 409


def test_routing_is_visible_and_overridable(client, auth):
    routed = create_payment(client, auth, method="bkash")
    assert routed["provider"] == "bkash"
    forced = create_payment(client, auth, method="bkash", provider="sslcommerz")
    assert forced["provider"] == "sslcommerz"
    assert forced["fee_minor"] == 3_750  # 2.5 %
    stripe = create_payment(client, auth, method="card", currency="USD", amount_minor=10_000)
    assert stripe["provider"] == "stripe"
    assert stripe["amount"] == "$100.00"


def test_unroutable_payment_is_422(client, auth):
    response = client.post(
        "/v1/payments",
        json={"amount_minor": 1_000, "currency": "BDT", "method": "paypal"},
        headers=auth,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_validation_errors_are_structured(client, auth):
    response = client.post(
        "/v1/payments",
        json={"amount_minor": -5, "currency": "BDT", "method": "bkash"},
        headers=auth,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_get_unknown_payment_is_404_and_merchants_are_isolated(client, auth, app):
    from app.core.security import generate_api_key
    from app.models import Merchant

    payment = create_payment(client, auth)
    assert client.get("/v1/payments/pay_doesnotexist", headers=auth).status_code == 404

    other_key, prefix, key_hash = generate_api_key("test")
    with app.state.session_factory() as db:
        db.add(
            Merchant(
                name="Other Shop",
                api_key_prefix=prefix,
                api_key_hash=key_hash,
                webhook_secret="x",
            )
        )
        db.commit()
    other = client.get(f"/v1/payments/{payment['id']}", headers={"X-API-Key": other_key})
    assert other.status_code == 404
