"""The shared controlled clock (tests/controlled_clock.py) behaves as the tests that rely on it assume."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta, timezone

import pytest
from controlled_clock import DEFAULT_START, ControlledClock


def _module_with_a_datetime_name() -> types.ModuleType:
    module = types.ModuleType("code_under_test")
    module.datetime = datetime  # type: ignore[attr-defined]
    return module


def test_a_clock_starts_at_its_start_and_stands_still_until_moved():
    clock = ControlledClock()

    assert clock.instant == DEFAULT_START
    assert clock.now(UTC) == clock.now(UTC) == DEFAULT_START  # no ticking between calls


def test_now_follows_the_datetime_convention_aware_with_a_zone_and_naive_utc_without():
    clock = ControlledClock(datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))

    assert clock.now(UTC) == datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert clock.now(timezone(timedelta(hours=-6))) == clock.now(UTC)  # same instant, other zone
    assert clock.now(timezone(timedelta(hours=-6))).utcoffset() == timedelta(hours=-6)
    assert clock.now() == datetime(2030, 1, 2, 3, 4, 5) and clock.now().tzinfo is None  # noqa: DTZ001 -- naive on purpose


def test_advance_moves_time_forward_and_returns_the_new_instant():
    clock = ControlledClock()

    assert clock.advance(timedelta(minutes=30)) == DEFAULT_START + timedelta(minutes=30)
    assert clock.advance(timedelta(seconds=1)) == DEFAULT_START + timedelta(minutes=30, seconds=1)
    assert clock.instant == DEFAULT_START + timedelta(minutes=30, seconds=1)


def test_time_never_runs_backwards_by_accident():
    clock = ControlledClock()

    with pytest.raises(ValueError, match="only advances"):
        clock.advance(timedelta(seconds=-1))
    assert clock.instant == DEFAULT_START  # unchanged by the refused move


def test_set_and_reset_jump_explicitly():
    clock = ControlledClock()
    elsewhere = datetime(1999, 12, 31, 23, 59, tzinfo=UTC)

    clock.set(elsewhere)
    assert clock.instant == elsewhere
    clock.advance(timedelta(minutes=5))
    clock.reset()
    assert clock.instant == DEFAULT_START


def test_a_naive_instant_is_refused_because_it_is_ambiguous():
    with pytest.raises(ValueError, match="timezone-aware"):
        ControlledClock(datetime(2026, 1, 1))  # noqa: DTZ001 -- naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        ControlledClock().set(datetime(2026, 1, 1))  # noqa: DTZ001 -- naive on purpose


def test_controlling_makes_the_module_read_the_clock_and_restores_it_afterwards():
    module = _module_with_a_datetime_name()
    clock = ControlledClock()

    with clock.controlling(module):
        assert module.datetime.now(UTC) == DEFAULT_START
        clock.advance(timedelta(hours=1))
        assert module.datetime.now(UTC) == DEFAULT_START + timedelta(hours=1)  # follows the clock live
        assert module.datetime.utcnow() == (DEFAULT_START + timedelta(hours=1)).replace(tzinfo=None)
        assert module.datetime.today() == clock.now()
    assert module.datetime is datetime  # restored


def test_controlling_restores_the_module_even_when_the_test_body_raises():
    module = _module_with_a_datetime_name()

    with pytest.raises(RuntimeError), ControlledClock().controlling(module):
        raise RuntimeError("boom")
    assert module.datetime is datetime


def test_real_datetimes_still_look_like_datetimes_to_the_module_under_test():
    module = _module_with_a_datetime_name()

    with ControlledClock().controlling(module):
        assert isinstance(datetime(2020, 1, 1, tzinfo=UTC), module.datetime)  # what the service's isinstance() checks rely on
        assert not isinstance("2020-01-01", module.datetime)
        assert module.datetime(2020, 1, 1, tzinfo=UTC) == datetime(2020, 1, 1, tzinfo=UTC)  # construction is untouched
        assert module.datetime.fromisoformat("2020-01-01T00:00:00+00:00") == datetime(2020, 1, 1, tzinfo=UTC)


def test_the_real_wall_clock_is_invisible_while_controlling():
    module = _module_with_a_datetime_name()
    far_from_today = datetime(1999, 12, 31, tzinfo=UTC)
    clock = ControlledClock(far_from_today)

    with clock.controlling(module):
        assert module.datetime.now(UTC) == far_from_today  # not "roughly now": exactly the controlled instant
    assert abs(module.datetime.now(UTC) - far_from_today) > timedelta(days=365)  # and it is the real one again


def test_a_module_without_a_datetime_name_is_refused_rather_than_silently_left_uncontrolled():
    module = types.ModuleType("refactored_to_import_datetime")  # no `datetime` attribute

    with pytest.raises(AttributeError), ControlledClock().controlling(module):
        pytest.fail("must not run the body when nothing could be controlled")


def test_several_modules_can_be_controlled_together():
    first, second = _module_with_a_datetime_name(), _module_with_a_datetime_name()

    with ControlledClock().controlling(first, second):
        assert first.datetime.now(UTC) == second.datetime.now(UTC) == DEFAULT_START
    assert first.datetime is datetime and second.datetime is datetime
