"""Payment lifecycle state machine.

CREATED -> AUTHORIZED -> CAPTURED -> (PARTIALLY_REFUNDED -> REFUNDED)
Any non-terminal state may move to FAILED; CREATED/AUTHORIZED may be CANCELLED.
"""

from __future__ import annotations

from enum import Enum


class PaymentStatus(str, Enum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[PaymentStatus] = frozenset(
    {PaymentStatus.REFUNDED, PaymentStatus.FAILED, PaymentStatus.CANCELLED}
)

SETTLEABLE_STATES: frozenset[PaymentStatus] = frozenset(
    {PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}
)

REFUNDABLE_STATES: frozenset[PaymentStatus] = frozenset(
    {PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED}
)

_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset(
        {
            PaymentStatus.AUTHORIZED,
            PaymentStatus.CAPTURED,  # auto-capture providers settle in one step
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.AUTHORIZED: frozenset(
        {PaymentStatus.CAPTURED, PaymentStatus.FAILED, PaymentStatus.CANCELLED}
    ),
    PaymentStatus.CAPTURED: frozenset({PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset(
        {PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}
    ),
    PaymentStatus.REFUNDED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.CANCELLED: frozenset(),
}


class InvalidTransitionError(Exception):
    """Raised when a payment is asked to move to a state it cannot reach."""

    def __init__(self, current: PaymentStatus, target: PaymentStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition payment from {current.value} to {target.value}")


def allowed_transitions(current: PaymentStatus) -> frozenset[PaymentStatus]:
    return _TRANSITIONS[current]


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in _TRANSITIONS[current]


def assert_transition(current: PaymentStatus, target: PaymentStatus) -> PaymentStatus:
    """Return `target` if the move is legal, otherwise raise InvalidTransitionError."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
    return target


def is_terminal(status: PaymentStatus) -> bool:
    return status in TERMINAL_STATES


def refund_target_status(amount_minor: int, refunded_minor_after: int) -> PaymentStatus:
    """Status a captured payment lands in once `refunded_minor_after` has been refunded."""
    if refunded_minor_after > amount_minor:
        raise ValueError("refunded amount exceeds payment amount")
    if refunded_minor_after == amount_minor:
        return PaymentStatus.REFUNDED
    return PaymentStatus.PARTIALLY_REFUNDED
