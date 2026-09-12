"""Tests for agent/runtime/backoff.py -- exponential backoff with jitter
(Continuous Ingestion Phase F).

Migrated from the experimental agent/outbox_uploader.py's Backoff tests
(test_backoff_grows_exponentially_and_is_bounded,
test_backoff_reset_returns_to_base) -- the requirement (bounded
exponential growth, reset on success) is unchanged; only the
implementation moved.
"""

from __future__ import annotations

from agent.runtime.backoff import Backoff


def test_backoff_grows_exponentially_and_is_bounded():
    backoff = Backoff(base_seconds=1.0, max_seconds=10.0, multiplier=2.0, jitter_fraction=0.0)

    delays = [backoff.next_delay() for _ in range(6)]

    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]  # capped at max_seconds


def test_backoff_reset_returns_to_base():
    backoff = Backoff(base_seconds=1.0, max_seconds=10.0, multiplier=2.0, jitter_fraction=0.0)

    backoff.next_delay()
    backoff.next_delay()
    assert backoff.next_delay() == 4.0

    backoff.reset()

    assert backoff.next_delay() == 1.0


def test_jitter_stays_within_declared_fraction():
    # Deterministic "random" source so the jitter bound is exactly checkable.
    backoff = Backoff(base_seconds=10.0, max_seconds=100.0, multiplier=2.0, jitter_fraction=0.2, random_fn=lambda: 1.0)

    delay = backoff.next_delay()

    # random_fn always returning 1.0 -> jitter = (1*2-1)*base*0.2 = +0.2*base
    assert delay == 10.0 * 1.2


def test_jitter_never_makes_delay_negative():
    backoff = Backoff(base_seconds=1.0, max_seconds=10.0, multiplier=2.0, jitter_fraction=2.0, random_fn=lambda: 0.0)

    delay = backoff.next_delay()

    assert delay >= 0.0


def test_zero_jitter_fraction_returns_exact_delay():
    backoff = Backoff(base_seconds=3.0, max_seconds=30.0, multiplier=3.0, jitter_fraction=0.0)

    assert backoff.next_delay() == 3.0
    assert backoff.next_delay() == 9.0


def test_multiple_independent_backoff_instances_do_not_share_state():
    a = Backoff(base_seconds=1.0, jitter_fraction=0.0)
    b = Backoff(base_seconds=5.0, jitter_fraction=0.0)

    a.next_delay()
    a.next_delay()

    assert b.next_delay() == 5.0  # unaffected by a's progression
