from decimal import Decimal

import pytest

from app.core.money import (
    FeeRule,
    UnsupportedCurrencyError,
    decimal_to_minor,
    format_amount,
    minor_to_decimal,
    percentage_of,
)
from app.core.routing import NoRouteError, PaymentRouter
from app.providers.bkash import BkashAdapter
from app.providers.sslcommerz import SSLCommerzAdapter
from app.providers.stripe import StripeAdapter


def test_percentage_rounds_half_up_without_floats():
    # 1.85% of 1,500.00 BDT = 27.75 exactly
    assert percentage_of(150_000, 185) == 2_775
    # 1.85% of 0.27 BDT = 0.004995 -> rounds to 0.00 (0 paisa)
    assert percentage_of(27, 185) == 0
    # 2.5% of 0.20 BDT = 0.005 -> half-up to 0.01 (1 paisa)
    assert percentage_of(20, 250) == 1


def test_fee_rule_fixed_min_and_max():
    stripe_like = FeeRule(percent_bps=290, fixed_minor=30)
    assert stripe_like.compute(10_000) == 320  # $100 -> $2.90 + $0.30

    with_floor = FeeRule(percent_bps=250, min_fee_minor=200)
    assert with_floor.compute(1_000) == 200  # 2.5% of 10.00 = 0.25 < floor 2.00

    capped = FeeRule(percent_bps=185, max_fee_minor=5_000)
    assert capped.compute(100_000_000) == 5_000


def test_fee_rule_rejects_negative_components():
    with pytest.raises(ValueError):
        FeeRule(percent_bps=-1)
    with pytest.raises(ValueError):
        FeeRule(percent_bps=10, min_fee_minor=500, max_fee_minor=100)


def test_decimal_conversions_roundtrip():
    assert minor_to_decimal(150_000, "BDT") == Decimal("1500.00")
    assert decimal_to_minor(Decimal("1500.00"), "BDT") == 150_000
    assert decimal_to_minor("99.5", "USD") == 9_950
    assert decimal_to_minor("1200", "JPY") == 1_200
    with pytest.raises(ValueError):
        decimal_to_minor("1.005", "BDT")
    with pytest.raises(UnsupportedCurrencyError):
        decimal_to_minor("1", "XYZ")


def test_format_amount_uses_symbols():
    assert format_amount(150_000, "BDT") == "৳1,500.00"
    assert format_amount(1_234_56, "USD") == "$1,234.56"
    assert format_amount(-500, "GBP") == "-£5.00"


@pytest.fixture
def router() -> PaymentRouter:
    providers = [BkashAdapter("s1"), SSLCommerzAdapter("s2"), StripeAdapter("s3")]
    return PaymentRouter({p.name: p for p in providers})


def test_router_picks_cheapest_capable_provider(router: PaymentRouter):
    # Both bKash direct (1.85%) and SSLCommerz (2.5%) accept bkash/BDT: direct is cheaper.
    quote = router.route(150_000, "BDT", "bkash")
    assert quote.provider == "bkash"
    assert quote.fee_minor == 2_775

    # Cards in BDT only go through SSLCommerz.
    assert router.route(150_000, "BDT", "card").provider == "sslcommerz"
    # Cards in USD only go through Stripe.
    usd = router.route(10_000, "usd", "CARD")
    assert usd.provider == "stripe"
    assert usd.fee_minor == 320


def test_router_quotes_are_sorted_and_preferred_provider_honoured(router: PaymentRouter):
    quotes = router.quotes(150_000, "BDT", "bkash")
    assert [q.provider for q in quotes] == ["bkash", "sslcommerz"]
    assert quotes[0].fee_minor <= quotes[1].fee_minor
    assert router.route(150_000, "BDT", "bkash", preferred="sslcommerz").provider == "sslcommerz"


def test_router_raises_when_no_provider_supports_pair(router: PaymentRouter):
    with pytest.raises(NoRouteError):
        router.route(10_000, "BDT", "paypal")
    with pytest.raises(NoRouteError):
        router.route(10_000, "USD", "bkash")
    with pytest.raises(NoRouteError):
        router.route(10_000, "BDT", "card", preferred="stripe")
