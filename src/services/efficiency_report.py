"""A sorter's Efficiency figures for a range of days: arithmetic, and nothing
else. Pure: no database, no clock, no request.

    calculate_efficiency(dates, checkin_days, settings) -> EfficiencyReport

WHAT IS ESTIMATED. How much staff time it would take to do by hand what the
sorter did, and what that time is worth -- beside what the sorter costs to
keep:

    staff_time_equivalent_hours   check-ins / manual_items_per_hour
    labor_value_equivalent        check-ins / manual_items_per_hour * labor_rate
    recurring_cost                recurring_annual_cost / 365 * in-service days
    net_operational_value         labor_value_equivalent - recurring_cost
    recurring_cost_per_item       recurring_cost / check-ins            (none when there were no check-ins)

It is a MANUAL EQUIVALENT. Nothing here is a machine's processing rate or its
running time, nothing is "hours saved", and none of it says staff time was
removed from anyone's schedule. A check-in is a processing event, not a
distinct item. There is no annualized figure, payback, return or anything
since installation here.

WHICH DAYS COUNT. The settings may say when the sorter went into service.
The days of the range on or after that date are its IN-SERVICE days, and
both sides of the sum use exactly those days: the recurring cost is charged
for them, and only THEIR check-ins are counted. A range that starts before
the sorter was in service is therefore reported from the day it was -- never
the whole range's check-ins against part of the range's cost. `checkin_count`
is still every check-in of the range, as every other report of the sorter
counts them, so the two can be told apart when they differ. A date after the
whole range leaves no in-service day: nothing counted, nothing charged.

With NO in-service date every day of the range is taken as in service --
there is nothing else to go on -- and the date is listed as missing, so that
is shown as an assumption and not as a fact.

UNKNOWN IS NOT ZERO. A figure whose inputs are not all known is None; the
others are still worked out. `missing` names every assumption this report
uses that nobody has set. A cost that was set to zero is a cost of zero.

EXACT, THEN ROUNDED ONCE. Everything is Decimal, carried at far more digits
than any result keeps, and each result is rounded once, half up: hours and
money to 0.01, cost per item to 0.0001. One figure is made from results
rather than from scratch: net_operational_value is the rounded labor value
less the rounded recurring cost, so the three figures a person reads add up
to the cent.

Standard library and services.efficiency_settings (for its types) only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Context, Decimal

from services.efficiency_settings import EffectiveEfficiencySettings

# A year, for charging a yearly cost by the day. Fixed: a day of a leap year costs what any other day does.
DAYS_PER_YEAR = 365

HOURS_PLACES = Decimal("0.01")
MONEY_PLACES = Decimal("0.01")
COST_PER_ITEM_PLACES = Decimal("0.0001")

# The assumptions this report uses, in the order `missing` lists them. one_time_cost is not one: nothing here
# depends on it.
USED_SETTINGS: tuple[str, ...] = ("manual_items_per_hour", "labor_rate", "recurring_annual_cost", "in_service_date")

# Far more digits than the largest value times the smallest step needs, and independent of whatever the process's
# own decimal context happens to be.
_EXACT = Context(prec=60, rounding=ROUND_HALF_UP)


def _rounded(value: Decimal, places: Decimal) -> Decimal:
    return _EXACT.quantize(value, places)


@dataclass(frozen=True, slots=True)
class EfficiencyReport:
    """A sorter's Efficiency figures over a range. Each Decimal is already
    rounded to the places it is reported in; None is a figure that could not
    be worked out from what is known.

    `checkin_count` is every check-in of the range. `in_service_days` and
    `in_service_checkin_count` are the days, and the check-ins on them, that
    the figures are about."""

    checkin_count: int
    in_service_days: int
    in_service_checkin_count: int
    staff_time_equivalent_hours: Decimal | None
    labor_value_equivalent: Decimal | None
    recurring_cost: Decimal | None
    net_operational_value: Decimal | None
    recurring_cost_per_item: Decimal | None
    missing: tuple[str, ...]


def calculate_efficiency(
    dates: Sequence[date], checkin_days: Sequence[int], settings: EffectiveEfficiencySettings
) -> EfficiencyReport:
    """The Efficiency figures for a range. `dates` are its calendar dates in
    order and `checkin_days` the check-ins on each -- exactly what the
    sorter's other reports count for those days. `settings` is what applies
    to the sorter (services.efficiency_settings.resolve_efficiency_settings).

    Raises ValueError if the two sequences do not line up or a count is not a
    whole number that could be a count: an answer that is not about the range
    it claims to be is not given.
    """
    if len(dates) != len(checkin_days):
        raise ValueError("there must be one check-in count for each date of the range")
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in checkin_days):
        raise ValueError("a check-in count must be a whole number that is not negative")

    in_service_date = settings.in_service_date
    in_service = [count for day, count in zip(dates, checkin_days, strict=True) if in_service_date is None or day >= in_service_date]
    in_service_days = len(in_service)
    checkins = sum(in_service)

    manual_rate = settings.manual_items_per_hour
    labor_rate = settings.labor_rate
    annual_cost = settings.recurring_annual_cost

    hours = None if manual_rate is None else _EXACT.divide(Decimal(checkins), manual_rate.value)
    labor_value = None if hours is None or labor_rate is None else _EXACT.multiply(hours, labor_rate.value)
    recurring_cost = None if annual_cost is None else _EXACT.divide(_EXACT.multiply(annual_cost, Decimal(in_service_days)), Decimal(DAYS_PER_YEAR))

    rounded_value = None if labor_value is None else _rounded(labor_value, MONEY_PLACES)
    rounded_cost = None if recurring_cost is None else _rounded(recurring_cost, MONEY_PLACES)

    known = {
        "manual_items_per_hour": manual_rate,
        "labor_rate": labor_rate,
        "recurring_annual_cost": annual_cost,
        "in_service_date": in_service_date,
    }
    return EfficiencyReport(
        checkin_count=sum(checkin_days),
        in_service_days=in_service_days,
        in_service_checkin_count=checkins,
        staff_time_equivalent_hours=None if hours is None else _rounded(hours, HOURS_PLACES),
        labor_value_equivalent=rounded_value,
        recurring_cost=rounded_cost,
        # The two figures as reported, so that what is shown adds up to the cent.
        net_operational_value=None if rounded_value is None or rounded_cost is None else _EXACT.subtract(rounded_value, rounded_cost),
        recurring_cost_per_item=(
            None
            if recurring_cost is None or checkins == 0
            else _rounded(_EXACT.divide(recurring_cost, Decimal(checkins)), COST_PER_ITEM_PLACES)
        ),
        missing=tuple(name for name in USED_SETTINGS if known[name] is None),
    )
