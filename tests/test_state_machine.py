import pytest

from app.core.state_machine import (
    TERMINAL_STATES,
    InvalidTransitionError,
    PaymentStatus,
    allowed_transitions,
    assert_transition,
    can_transition,
    is_terminal,
    refund_target_status,
)


def test_happy_path_created_to_refunded():
    status = PaymentStatus.CREATED
    for target in (
        PaymentStatus.AUTHORIZED,
        PaymentStatus.CAPTURED,
        PaymentStatus.PARTIALLY_REFUNDED,
        PaymentStatus.REFUNDED,
    ):
        status = assert_transition(status, target)
    assert status is PaymentStatus.REFUNDED


def test_auto_capture_skips_authorized():
    assert can_transition(PaymentStatus.CREATED, PaymentStatus.CAPTURED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PaymentStatus.CREATED, PaymentStatus.REFUNDED),
        (PaymentStatus.AUTHORIZED, PaymentStatus.PARTIALLY_REFUNDED),
        (PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED),
        (PaymentStatus.CAPTURED, PaymentStatus.CANCELLED),
        (PaymentStatus.REFUNDED, PaymentStatus.CAPTURED),
        (PaymentStatus.FAILED, PaymentStatus.AUTHORIZED),
        (PaymentStatus.CANCELLED, PaymentStatus.CAPTURED),
    ],
)
def test_invalid_transitions_raise(current, target):
    with pytest.raises(InvalidTransitionError) as exc_info:
        assert_transition(current, target)
    assert exc_info.value.current is current
    assert exc_info.value.target is target
    assert current.value in str(exc_info.value)


def test_terminal_states_have_no_exits():
    for status in TERMINAL_STATES:
        assert is_terminal(status)
        assert allowed_transitions(status) == frozenset()


def test_partial_refund_can_repeat_then_complete():
    assert can_transition(PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.PARTIALLY_REFUNDED)
    assert can_transition(PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED)


def test_refund_target_status():
    assert refund_target_status(10_000, 2_500) is PaymentStatus.PARTIALLY_REFUNDED
    assert refund_target_status(10_000, 10_000) is PaymentStatus.REFUNDED
    with pytest.raises(ValueError):
        refund_target_status(10_000, 10_001)
