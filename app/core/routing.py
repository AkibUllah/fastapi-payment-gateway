"""Smart routing: choose the cheapest provider that supports a currency/method pair."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.core.money import FeeRule
from app.providers.base import PaymentProvider

# Illustrative sandbox fee table (provider -> currency -> rule). Percentages are in
# basis points; fixed parts are in minor units. Real contracts are negotiated per
# merchant and should be loaded from configuration, not hard-coded.
DEFAULT_FEE_TABLE: dict[str, dict[str, FeeRule]] = {
    "bkash": {"BDT": FeeRule(percent_bps=185)},
    "sslcommerz": {"BDT": FeeRule(percent_bps=250, min_fee_minor=200)},
    "stripe": {
        "USD": FeeRule(percent_bps=290, fixed_minor=30),
        "EUR": FeeRule(percent_bps=290, fixed_minor=30),
        "GBP": FeeRule(percent_bps=290, fixed_minor=30),
    },
    # Deliberately expensive so the mock is only routed to when asked for explicitly.
    "mock": {
        "BDT": FeeRule(percent_bps=500),
        "USD": FeeRule(percent_bps=500),
    },
}


class NoRouteError(Exception):
    def __init__(self, currency: str, method: str) -> None:
        self.currency = currency
        self.method = method
        super().__init__(f"No provider supports {method} payments in {currency}")


@dataclass(frozen=True)
class FeeQuote:
    provider: str
    fee_minor: int
    rule: FeeRule


class PaymentRouter:
    """Pick a provider for (amount, currency, method), cheapest fee first.

    Ties are broken by the order providers were registered, so the operator can
    express a preference by registration order.
    """

    def __init__(
        self,
        providers: Mapping[str, PaymentProvider],
        fee_table: Mapping[str, Mapping[str, FeeRule]] | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._fee_table = fee_table if fee_table is not None else DEFAULT_FEE_TABLE

    @property
    def providers(self) -> Mapping[str, PaymentProvider]:
        return self._providers

    def fee_rule(self, provider: str, currency: str) -> FeeRule | None:
        return self._fee_table.get(provider, {}).get(currency.upper())

    def quotes(self, amount_minor: int, currency: str, method: str) -> list[FeeQuote]:
        currency = currency.upper()
        method = method.lower()
        quotes: list[FeeQuote] = []
        for name, provider in self._providers.items():
            if not provider.supports(currency, method):
                continue
            rule = self.fee_rule(name, currency)
            if rule is None:
                continue
            quotes.append(FeeQuote(provider=name, fee_minor=rule.compute(amount_minor), rule=rule))
        order = {name: i for i, name in enumerate(self._providers)}
        quotes.sort(key=lambda q: (q.fee_minor, order[q.provider]))
        return quotes

    def route(
        self,
        amount_minor: int,
        currency: str,
        method: str,
        preferred: str | None = None,
    ) -> FeeQuote:
        quotes = self.quotes(amount_minor, currency, method)
        if preferred:
            for quote in quotes:
                if quote.provider == preferred:
                    return quote
            raise NoRouteError(currency, f"{method} via {preferred}")
        if not quotes:
            raise NoRouteError(currency, method)
        return quotes[0]

    def supported_pairs(self) -> Iterable[tuple[str, str, str]]:
        for name, provider in self._providers.items():
            for currency in sorted(provider.supported_currencies):
                for method in sorted(provider.supported_methods):
                    if self.fee_rule(name, currency) is not None:
                        yield name, currency, method
