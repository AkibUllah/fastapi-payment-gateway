import pytest

from app.config import Settings
from app.core.backoff import DEFAULT_RETRY_SCHEDULE, next_delay, parse_schedule


def test_each_retry_moves_to_the_next_step_then_the_schedule_ends():
    delays = [next_delay((60, 300, 1800), used) for used in range(4)]
    assert delays == [60, 300, 1800, None]


@pytest.mark.parametrize(
    ("roll", "expected"),
    [(0.0, 90.0), (0.5, 100.0), (1.0, 110.0)],
)
def test_jitter_spreads_a_step_evenly_around_its_length(roll, expected):
    assert next_delay((100,), 0, jitter=0.1, rng=lambda: roll) == pytest.approx(expected)


def test_jitter_is_never_rolled_past_the_end_of_the_schedule():
    def explode() -> float:
        raise AssertionError("no delay to jitter")

    assert next_delay((), 0, jitter=0.5, rng=explode) is None


def test_negative_retry_count_is_a_programming_error():
    with pytest.raises(ValueError):
        next_delay((60,), -1)


def test_parse_schedule_accepts_spaces_and_empty_disables_retries():
    assert parse_schedule("60, 300 ,1800") == (60, 300, 1800)
    assert parse_schedule("") == ()


@pytest.mark.parametrize("value", ["60,-5", "0", "soon"])
def test_parse_schedule_rejects_bad_steps(value):
    with pytest.raises(ValueError):
        parse_schedule(value)


def test_default_settings_follow_the_published_schedule():
    settings = Settings(_env_file=None)
    assert settings.webhook_retry_steps == DEFAULT_RETRY_SCHEDULE == (60, 300, 1800, 7200, 43200)


@pytest.mark.parametrize(
    "overrides",
    [
        {"webhook_retry_schedule": "60,nope"},
        {"webhook_retry_jitter": 1.5},
        {"webhook_lease_seconds": 0},
        {"webhook_dispatch_batch_size": 0},
        # A lease shorter than one HTTP attempt would hand live rows to a second worker.
        {"webhook_timeout_seconds": 30, "webhook_lease_seconds": 60},
    ],
)
def test_invalid_retry_settings_are_rejected(overrides):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)
