"""One controlled clock for time-sensitive tests.

WHY THIS EXISTS. Code that decides "has this expired?" reads the wall clock
(`datetime.now(UTC)`). A test that stamps its fixtures with a FIXED instant but
lets that code read the REAL clock passes only until the real calendar moves
past the fixed instant plus whatever lifetime the fixture has -- and then fails
on a day nobody touched anything. (This is exactly how the enrollment tests
broke in September 2026.)

The cure is that every side of such a test agrees on ONE clock, which only the
test moves:

    from controlled_clock import ControlledClock

    CLOCK = ControlledClock()

    @pytest.fixture(autouse=True)
    def _controlled_time():
        CLOCK.reset()
        with CLOCK.controlling(service_module):   # the code under test now reads CLOCK
            yield

    code = generate(now=CLOCK.now())      # fixtures are stamped from the clock ...
    CLOCK.advance(timedelta(minutes=31))  # ... and expiry is reached by moving it,
    with pytest.raises(Expired):          # never by picking a "long ago" date.
        redeem(code)

The start instant below is arbitrary: nothing may depend on it matching the
real date, and `controlling()` is what makes that true -- the code under test
cannot see the real clock while it is in effect. (tests/test_controlled_clock.py
and the enrollment tests prove it by running from years before and after the
real date.)

Only `datetime.now()` / `utcnow()` / `today()` are controlled. Everything else on
`datetime` behaves exactly as the real class, and real `datetime` instances still
satisfy `isinstance(x, module.datetime)`, so the module under test is otherwise
unaware. Naive values are UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from types import ModuleType
from unittest.mock import patch

# An arbitrary fixed instant. It is deliberately NOT named like "now": it is where a
# controlled clock STARTS, not what any code is told the current time is.
DEFAULT_START = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


class _AnyRealDatetime(type):
    """Lets the patched `datetime` keep answering isinstance() for genuine datetimes."""

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)


class ControlledClock:
    """A clock that only moves when the test says so."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        self._start = self._require_aware(start)
        self._now = self._start

    @staticmethod
    def _require_aware(instant: datetime) -> datetime:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("a controlled clock needs a timezone-aware instant (naive times are ambiguous)")
        return instant

    def now(self, tz: tzinfo | None = None) -> datetime:
        """The controlled instant. Like datetime.now(): aware in `tz` when given, else naive (UTC here)."""
        if tz is not None:
            return self._now.astimezone(tz)
        return self._now.astimezone(UTC).replace(tzinfo=None)

    @property
    def instant(self) -> datetime:
        """The controlled instant as an aware UTC datetime (what fixtures are stamped from)."""
        return self._now.astimezone(UTC)

    def advance(self, delta: timedelta) -> datetime:
        """Moves time FORWARD by `delta` (never backwards -- use set()/reset() for that) and returns the new instant."""
        if delta < timedelta(0):
            raise ValueError("a controlled clock only advances; use set() or reset() to jump")
        self._now = self._now + delta
        return self.instant

    def set(self, instant: datetime) -> None:
        self._now = self._require_aware(instant)

    def reset(self) -> None:
        self._now = self._start

    def datetime_class(self) -> type[datetime]:
        """A stand-in for `datetime` whose now()/utcnow()/today() read this clock."""
        clock = self

        class ControlledDatetime(datetime, metaclass=_AnyRealDatetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
                return clock.now(tz)

            @classmethod
            def utcnow(cls) -> datetime:  # type: ignore[override]
                return clock.now(UTC).replace(tzinfo=None)

            @classmethod
            def today(cls) -> datetime:  # type: ignore[override]
                return clock.now()

        return ControlledDatetime

    @contextmanager
    def controlling(self, *modules: ModuleType, attribute: str = "datetime") -> Iterator[ControlledClock]:
        """While active, each module's `datetime` name is this clock's stand-in, so code in that
        module that calls `datetime.now(...)` reads THIS clock. Raises AttributeError (rather than
        silently controlling nothing) if a module has no such name -- e.g. it was refactored to
        `import datetime`. Always restored on exit, including on an exception."""
        stand_in = self.datetime_class()
        with ExitStack() as stack:
            for module in modules:
                stack.enter_context(patch.object(module, attribute, stand_in))
            yield self
