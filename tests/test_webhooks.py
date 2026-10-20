import json

from app.core.security import verify_signature_header
from app.services.webhooks import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER
from tests.conftest import create_payment


def test_outgoing_webhooks_are_signed_and_verifiable(client, auth, merchant, sink):
    payment = create_payment(client, auth)

    assert len(sink.requests) == 1
    request = sink.requests[0]
    assert str(request.url) == "https://merchant.example.com/payments/webhook"
    assert request.headers[EVENT_HEADER] == "payment.captured"
    assert request.headers[DELIVERY_HEADER].startswith("whk_")
    verify_signature_header(
        merchant["webhook_secret"], request.content, request.headers[SIGNATURE_HEADER]
    )

    payload = json.loads(request.content)
    assert payload["type"] == "payment.captured"
    assert payload["data"]["id"] == payment["id"]
    assert payload["data"]["status"] == "captured"

    deliveries = client.get("/v1/webhooks/deliveries", headers=auth).json()
    assert deliveries[0]["status"] == "delivered"
    assert deliveries[0]["response_status"] == 200
    assert deliveries[0]["attempts"] == 1


def test_outgoing_webhook_retries_then_marks_failed(client, auth, sink):
    sink.fail_next = 5  # more than max_attempts (3)
    create_payment(client, auth)
    assert len(sink.requests) == 3
    delivery = client.get("/v1/webhooks/deliveries", headers=auth).json()[0]
    assert delivery["status"] == "failed"
    assert delivery["attempts"] == 3
    assert delivery["last_error"] == "HTTP 503"


def test_outgoing_webhook_recovers_on_retry(client, auth, sink):
    sink.fail_next = 1
    create_payment(client, auth)
    delivery = client.get("/v1/webhooks/deliveries", headers=auth).json()[0]
    assert delivery["status"] == "delivered"
    assert delivery["attempts"] == 2


def test_inbound_bkash_webhook_captures_authorized_payment(client, auth, app, sink):
    payment = create_payment(client, auth, capture="manual")
    assert payment["status"] == "authorized"
    adapter = app.state.providers["bkash"]
    headers, body = adapter.build_webhook(payment["provider_ref"], "payment.captured")

    response = client.post("/v1/webhooks/bkash", content=body, headers=headers)
    assert response.status_code == 202, response.text
    ack = response.json()
    assert ack["result"] == "applied"
    assert ack["payment_id"] == payment["id"]

    current = client.get(f"/v1/payments/{payment['id']}", headers=auth).json()
    assert current["status"] == "captured"
    assert current["events"][-1]["source"] == "provider"
    # Merchant was notified of both the authorisation and the capture.
    assert [json.loads(r.content)["type"] for r in sink.requests] == [
        "payment.authorized",
        "payment.captured",
    ]


def test_inbound_webhook_is_idempotent_and_guards_terminal_states(client, auth, app):
    payment = create_payment(client, auth)  # already captured
    adapter = app.state.providers["bkash"]

    headers, body = adapter.build_webhook(payment["provider_ref"], "payment.captured")
    assert (
        client.post("/v1/webhooks/bkash", content=body, headers=headers).json()["result"]
        == "ignored"
    )

    headers, body = adapter.build_webhook(payment["provider_ref"], "payment.failed")
    assert (
        client.post("/v1/webhooks/bkash", content=body, headers=headers).json()["result"]
        == "conflict"
    )
    assert client.get(f"/v1/payments/{payment['id']}", headers=auth).json()["status"] == "captured"


def test_inbound_webhook_with_bad_signature_is_rejected(client, auth, app):
    payment = create_payment(client, auth, capture="manual")
    adapter = app.state.providers["bkash"]
    _headers, body = adapter.build_webhook(payment["provider_ref"], "payment.captured")

    bad = client.post("/v1/webhooks/bkash", content=body, headers={"X-Bkash-Signature": "00ff"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_signature"

    missing = client.post("/v1/webhooks/bkash", content=body)
    assert missing.status_code == 400
    assert (
        client.get(f"/v1/payments/{payment['id']}", headers=auth).json()["status"] == "authorized"
    )


def test_inbound_stripe_webhook_uses_timestamped_signature(client, auth, app):
    payment = create_payment(
        client, auth, method="card", currency="USD", amount_minor=2_000, capture="manual"
    )
    adapter = app.state.providers["stripe"]
    headers, body = adapter.build_webhook(payment["provider_ref"], "payment.captured")
    assert headers["Stripe-Signature"].startswith("t=")

    ok = client.post("/v1/webhooks/stripe", content=body, headers=headers)
    assert ok.status_code == 202
    assert ok.json()["result"] == "applied"

    stale = {"Stripe-Signature": headers["Stripe-Signature"].replace("t=", "t=1", 1)}
    assert client.post("/v1/webhooks/stripe", content=body, headers=stale).status_code == 400


def test_inbound_sslcommerz_failure_marks_payment_failed(client, auth, app):
    payment = create_payment(client, auth, method="card", capture="manual")
    adapter = app.state.providers["sslcommerz"]
    headers, body = adapter.build_webhook(payment["provider_ref"], "payment.failed")
    response = client.post("/v1/webhooks/sslcommerz", content=body, headers=headers)
    assert response.json()["result"] == "applied"
    current = client.get(f"/v1/payments/{payment['id']}", headers=auth).json()
    assert current["status"] == "failed"
    assert current["failure_reason"]


def test_inbound_webhook_for_unknown_provider_or_reference(client, app):
    assert client.post("/v1/webhooks/paypal", content=b"{}").status_code == 404
    adapter = app.state.providers["mock"]
    from app.core.security import hmac_hex

    body = json.dumps({"event": "payment.captured", "ref": "MOCK-nothing"}).encode()
    response = client.post(
        "/v1/webhooks/mock",
        content=body,
        headers={"X-Mock-Signature": hmac_hex(adapter.webhook_secret, body)},
    )
    assert response.status_code == 202
    assert response.json()["result"] == "unknown_reference"


def test_mock_provider_records_calls(client, auth, app):
    mock = app.state.providers["mock"]
    payment = create_payment(client, auth, method="mock", provider="mock", capture="manual")
    client.post(f"/v1/payments/{payment['id']}/capture", headers=auth)
    client.post(f"/v1/payments/{payment['id']}/refund", headers=auth)
    assert [c[0] for c in mock.calls] == ["authorize", "capture", "refund"]
