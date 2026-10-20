import csv
import io
from datetime import datetime, timezone

from tests.conftest import create_payment


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def test_run_settlement_groups_by_provider_and_nets_fees_and_refunds(client, auth):
    a = create_payment(client, auth, amount_minor=100_000)  # bkash, fee 1850
    b = create_payment(client, auth, amount_minor=50_000)  # bkash, fee 925
    create_payment(client, auth, method="card", amount_minor=20_000)  # sslcommerz, fee 500
    create_payment(client, auth, capture="manual")  # authorized only: not settleable
    client.post(f"/v1/payments/{b['id']}/refund", json={"amount_minor": 10_000}, headers=auth)

    response = client.post("/v1/settlements/run", json={"settlement_date": _today()}, headers=auth)
    assert response.status_code == 201, response.text
    batches = {s["provider"]: s for s in response.json()}
    assert set(batches) == {"bkash", "sslcommerz"}

    bkash = batches["bkash"]
    assert bkash["payment_count"] == 2
    assert bkash["gross_minor"] == 150_000
    assert bkash["fee_minor"] == 1_850 + 925
    assert bkash["refunded_minor"] == 10_000
    assert bkash["net_minor"] == 150_000 - 2_775 - 10_000
    assert bkash["net"] == "৳1,372.25"
    assert {line["payment_id"] for line in bkash["lines"]} == {a["id"], b["id"]}

    # Settled payments carry the batch id and are not picked up twice.
    assert (
        client.get(f"/v1/payments/{a['id']}", headers=auth).json()["settlement_id"] == bkash["id"]
    )
    rerun = client.post("/v1/settlements/run", json={"settlement_date": _today()}, headers=auth)
    assert rerun.json() == []


def test_list_settlements_with_filters_and_total_header(client, auth):
    create_payment(client, auth)
    create_payment(client, auth, method="card", currency="USD", amount_minor=5_000)
    client.post("/v1/settlements/run", json={"settlement_date": _today()}, headers=auth)

    everything = client.get("/v1/settlements", headers=auth)
    assert everything.headers["X-Total-Count"] == "2"
    only_stripe = client.get("/v1/settlements", params={"provider": "stripe"}, headers=auth)
    assert only_stripe.headers["X-Total-Count"] == "1"
    assert only_stripe.json()[0]["currency"] == "USD"
    assert client.get("/v1/settlements/set_missing", headers=auth).status_code == 404


def test_csv_export_has_lines_and_total_row(client, auth):
    create_payment(client, auth, amount_minor=100_000)
    create_payment(client, auth, amount_minor=25_000)
    settlement = client.post(
        "/v1/settlements/run", json={"settlement_date": _today()}, headers=auth
    ).json()[0]

    response = client.get(f"/v1/settlements/{settlement['id']}/export.csv", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 3
    assert rows[-1]["payment_id"] == "TOTAL"
    assert int(rows[-1]["gross_minor"]) == 125_000
    assert sum(int(r["net_minor"]) for r in rows[:-1]) == int(rows[-1]["net_minor"])


def test_reconcile_clean_against_provider_report(client, auth):
    create_payment(client, auth, amount_minor=100_000)
    create_payment(client, auth, amount_minor=40_000)
    settlement = client.post(
        "/v1/settlements/run", json={"settlement_date": _today()}, headers=auth
    ).json()[0]

    # No rows supplied -> pull the sandbox provider's own report, which matches the ledger.
    response = client.post(f"/v1/settlements/{settlement['id']}/reconcile", headers=auth)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["reconciled"] is True
    assert result["matched"] == 2
    assert result["ledger_net_minor"] == result["provider_net_minor"]

    fetched = client.get(f"/v1/settlements/{settlement['id']}", headers=auth).json()
    assert fetched["status"] == "reconciled"
    assert fetched["reconciled_at"] is not None

    paid = client.post(f"/v1/settlements/{settlement['id']}/mark-paid", headers=auth)
    assert paid.status_code == 200
    assert paid.json()["status"] == "paid"
    closed = client.post(f"/v1/settlements/{settlement['id']}/reconcile", headers=auth)
    assert closed.status_code == 409


def test_reconcile_reports_mismatches_missing_and_unexpected(client, auth):
    a = create_payment(client, auth, amount_minor=100_000)
    b = create_payment(client, auth, amount_minor=40_000)
    settlement = client.post(
        "/v1/settlements/run", json={"settlement_date": _today()}, headers=auth
    ).json()[0]

    rows = [
        {"provider_ref": a["provider_ref"], "amount_minor": 100_000, "fee_minor": 1_850},
        # b: provider claims a different fee
        {"provider_ref": b["provider_ref"], "amount_minor": 40_000, "fee_minor": 999},
        # a reference the ledger never saw
        {"provider_ref": "TRXGHOST", "amount_minor": 5_000, "fee_minor": 93},
    ]
    response = client.post(
        f"/v1/settlements/{settlement['id']}/reconcile", json={"rows": rows}, headers=auth
    )
    result = response.json()
    assert result["reconciled"] is False
    assert result["matched"] == 1
    assert result["amount_mismatches"] == [
        {
            "provider_ref": b["provider_ref"],
            "ledger_amount_minor": 40_000,
            "provider_amount_minor": 40_000,
            "ledger_fee_minor": 740,
            "provider_fee_minor": 999,
        }
    ]
    assert result["unexpected_in_provider"] == ["TRXGHOST"]
    assert result["missing_in_provider"] == []
    assert (
        client.get(f"/v1/settlements/{settlement['id']}", headers=auth).json()["status"]
        == "discrepancy"
    )

    missing = client.post(
        f"/v1/settlements/{settlement['id']}/reconcile", json={"rows": rows[:1]}, headers=auth
    ).json()
    assert missing["missing_in_provider"] == [b["provider_ref"]]
    assert (
        client.post(f"/v1/settlements/{settlement['id']}/mark-paid", headers=auth).status_code
        == 409
    )


def test_cli_run_settlement_and_create_merchant(app, client, auth, monkeypatch, capsys):
    from app import cli

    create_payment(client, auth, amount_minor=10_000)
    monkeypatch.setattr(
        cli, "_session_factory", lambda: (app.state.settings, app.state.session_factory)
    )

    assert (
        cli.main(
            ["create-merchant", "Chittagong Traders", "--webhook-url", "https://ct.example/wh"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "api_key:         pk_test_" in out

    assert cli.main(["run-settlement", "--date", _today()]) == 0
    out = capsys.readouterr().out
    assert "bkash BDT" in out and "payments=1" in out
