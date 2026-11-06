"""Retry schedule for merchant webhooks: fixed steps, spread out with jitter.

A merchant outage fails every in-flight delivery at once. Without jitter they would all come
back on the same second and hit the endpoint as it recovers.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence

DEFAULT_RETRY_SCHEDULE: tuple[int, ...] = (60, 300, 1800, 7200, 43200)


def next_delay(
    schedule: Sequence[int],
    retries_used: int,
    jitter: float = 0.0,
    rng: Callable[[], float] = random.random,
) -> float | None:
    """Seconds to wait before the next retry, or None once the schedule is exhausted.

    `jitter` is a fraction: 0.1 spreads each step evenly over +/-10% of its length.
    """
    if retries_used < 0:
        raise ValueError("retries_used cannot be negative")
    if retries_used >= len(schedule):
        return None
    return schedule[retries_used] * (1 + jitter * (2 * rng() - 1))


def parse_schedule(value: str) -> tuple[int, ...]:
    """Parse "60,300,1800" into seconds. An empty string disables scheduled retries."""
    steps = tuple(int(step) for step in value.replace(" ", "").split(",") if step)
    if any(step <= 0 for step in steps):
        raise ValueError("retry schedule steps must be positive seconds")
    return steps
