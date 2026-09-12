"""Exponential backoff with jitter (Continuous Ingestion Phase F).

Shared by the uploader and heartbeat loops. Originally written as a
fresh, small implementation local to this package rather than importing
the experimental agent/outbox_uploader.py's own Backoff class, which
transitively imported agent/outbox.py (SQLite) -- a dependency the
canonical runtime must never have (see tests/test_runtime_no_sqlite.py).
That experimental module has since been removed entirely (the cleanup/
convergence phase, once this package's coverage proved equivalent -- see
tests/test_runtime_backoff.py), so this is now simply this package's own
Backoff, not a substitute for anything still living elsewhere.
"""

from __future__ import annotations

import random
from collections.abc import Callable


class Backoff:
    """Reset on success, grows (capped) on repeated failure. Holds no
    reference to time.sleep -- callers decide how to wait (this package's
    loops sleep via a threading.Event.wait(delay), so a stop request
    interrupts a backoff sleep immediately instead of blocking shutdown).

    jitter_fraction adds up to +/-jitter_fraction of the base delay
    (uniformly, never negative overall) so many agents backing off at
    once don't all retry in lockstep against the backend.
    """

    def __init__(
        self,
        base_seconds: float = 2.0,
        max_seconds: float = 300.0,
        multiplier: float = 2.0,
        jitter_fraction: float = 0.2,
        random_fn: Callable[[], float] | None = None,
    ) -> None:
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.multiplier = multiplier
        self.jitter_fraction = jitter_fraction
        self._random = random_fn if random_fn is not None else random.random
        self._current = base_seconds

    def reset(self) -> None:
        self._current = self.base_seconds

    def next_delay(self) -> float:
        delay = self._current
        self._current = min(self._current * self.multiplier, self.max_seconds)

        if self.jitter_fraction <= 0:
            return delay

        jitter_range = delay * self.jitter_fraction
        jitter = (self._random() * 2 - 1) * jitter_range
        return max(0.0, delay + jitter)
