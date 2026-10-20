"""Money helpers.

All monetary amounts inside the gateway are integers in the currency's minor unit
(paisa for BDT, cents for USD). Decimal is used only at the API boundary for display.
Floats are never used for money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CURRENCY_EXPONENTS: dict[str, int] = {
    "BDT": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "INR": 2,
    "JPY": 0,
}

CURRENCY_SYMBOLS: dict[str, str] = {
    "BDT": "৳",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "INR": "₹",
    "JPY": "¥",
}

BASIS_POINTS = 10_000


class UnsupportedCurrencyError(ValueError):
    """Raised when a currency is not in the exponent table."""


def currency_exponent(currency: str) -> int:
    try:
        return CURRENCY_EXPONENTS[currency.upper()]
    except KeyError as exc:
        raise UnsupportedCurrencyError(f"Unsupported currency: {currency}") from exc


def minor_to_decimal(amount_minor: int, currency: str) -> Decimal:
    """Convert 150000 (BDT) -> Decimal('1500.00')."""
    exponent = currency_exponent(currency)
    return (Decimal(amount_minor) / (Decimal(10) ** exponent)).quantize(
        Decimal(1).scaleb(-exponent)
    )


def decimal_to_minor(amount: Decimal | str, currency: str) -> int:
    """Convert Decimal('1500.00') (BDT) -> 150000. Rejects sub-minor precision."""
    exponent = currency_exponent(currency)
    value = Decimal(str(amount))
    scaled = value.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} has more precision than {currency} allows")
    return int(scaled)


def format_amount(amount_minor: int, currency: str) -> str:
    """Human readable amount with symbol, e.g. '৳1,500.00'."""
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    exponent = currency_exponent(currency)
    value = minor_to_decimal(amount_minor, currency)
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.{exponent}f}"


def round_half_up_div(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero, without touching floats."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    quotient = (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return int(quotient)


def percentage_of(amount_minor: int, basis_points: int) -> int:
    """`basis_points` of `amount_minor`, half-up rounded. 185 bps == 1.85 %."""
    return round_half_up_div(amount_minor * basis_points, BASIS_POINTS)


@dataclass(frozen=True)
class FeeRule:
    """A provider fee: percentage (in basis points) + fixed component, with optional caps."""

    percent_bps: int
    fixed_minor: int = 0
    min_fee_minor: int = 0
    max_fee_minor: int | None = None

    def __post_init__(self) -> None:
        if self.percent_bps < 0 or self.fixed_minor < 0 or self.min_fee_minor < 0:
            raise ValueError("fee components cannot be negative")
        if self.max_fee_minor is not None and self.max_fee_minor < self.min_fee_minor:
            raise ValueError("max_fee_minor cannot be below min_fee_minor")

    def compute(self, amount_minor: int) -> int:
        if amount_minor < 0:
            raise ValueError("amount cannot be negative")
        fee = percentage_of(amount_minor, self.percent_bps) + self.fixed_minor
        fee = max(fee, self.min_fee_minor)
        if self.max_fee_minor is not None:
            fee = min(fee, self.max_fee_minor)
        return fee

    def describe(self) -> str:
        parts = [f"{self.percent_bps / 100:.2f}%"]
        if self.fixed_minor:
            parts.append(f"+{self.fixed_minor} minor")
        if self.min_fee_minor:
            parts.append(f"min {self.min_fee_minor}")
        if self.max_fee_minor is not None:
            parts.append(f"max {self.max_fee_minor}")
        return " ".join(parts)
