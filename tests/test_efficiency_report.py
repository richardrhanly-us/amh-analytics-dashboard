"""Reports R6C: a sorter's Efficiency figures -- the pure arithmetic.

    calculate_efficiency(dates, checkin_days, settings) -> EfficiencyReport

Nothing here touches a database, a clock or a request. The route that feeds
it is tested in tests/test_customer_api_efficiency_report.py.

Imported the "flat" way (services.efficiency_report), the identity the API
process uses.
"""

from __future__ import annotations

import dataclasses
import decimal
import inspect
from datetime import date, timedelta
from decimal import Decimal

import pytest

from services import efficiency_report
from services.efficiency_report import (
    DAYS_PER_YEAR,
    USED_SETTINGS,
    EfficiencyReport,
    calculate_efficiency,
)
from services.efficiency_settings import (
    OrganizationEfficiencySettings,
    SorterEfficiencySettings,
    parse_organization_efficiency_settings,
    parse_sorter_efficiency_settings,
    resolve_efficiency_settings,
)

D = Decimal


def _dates(first: str, days: int) -> list[date]:
    start = date.fromisoformat(first)
    return [start + timedelta(days=offset) for offset in range(days)]


# September 2026: 30 days, 3,140 check-ins -- 100 on each of the first 29 days and 240 on the last.
SEPTEMBER = _dates("2026-09-01", 30)
SEPTEMBER_CHECKINS = [100] * 29 + [240]


def _settings(organization: dict | None = None, **sorter):
    """What applies to a sorter whose organization and own Efficiency blocks are these, through the real model."""
    return resolve_efficiency_settings(
        parse_organization_efficiency_settings({"efficiency": organization or {}}),
        parse_sorter_efficiency_settings({"efficiency": sorter}),
    )


FULL = {"manual_items_per_hour": "45.0", "labor_rate": "17.56", "recurring_annual_cost": "8400.00", "in_service_date": "2020-11-20"}


def _report(dates=SEPTEMBER, checkins=SEPTEMBER_CHECKINS, organization=None, **sorter) -> EfficiencyReport:
    return calculate_efficiency(dates, checkins, _settings(organization, **sorter))


def _figures(report: EfficiencyReport) -> tuple:
    return (
        report.staff_time_equivalent_hours,
        report.labor_value_equivalent,
        report.recurring_cost,
        report.net_operational_value,
        report.recurring_cost_per_item,
    )


def _text(report: EfficiencyReport) -> tuple:
    return tuple(None if figure is None else str(figure) for figure in _figures(report))


# =====================================================================================================================
# The ordinary case
# =====================================================================================================================

def test_a_fully_configured_sorter():
    report = _report(**FULL, one_time_cost="118003.92")

    assert report == EfficiencyReport(
        checkin_count=3140,
        in_service_days=30,
        in_service_checkin_count=3140,
        staff_time_equivalent_hours=D("69.78"),       # 3140 / 45 = 69.777...
        labor_value_equivalent=D("1225.30"),          # 3140 / 45 * 17.56 = 1225.2977...
        recurring_cost=D("690.41"),                   # 8400 / 365 * 30 = 690.4109...
        net_operational_value=D("534.89"),            # 1225.30 - 690.41
        recurring_cost_per_item=D("0.2199"),          # 690.4109... / 3140 = 0.21987...
        missing=(),
    )
    assert _text(report) == ("69.78", "1225.30", "690.41", "534.89", "0.2199")


def test_the_figures_are_the_formulas_and_nothing_else():
    report = _report(**FULL)
    with decimal.localcontext() as context:
        context.prec = 50
        hours = D(3140) / D("45.0")
        cost = D("8400.00") / D(365) * D(30)

        assert report.staff_time_equivalent_hours == hours.quantize(D("0.01"), decimal.ROUND_HALF_UP)
        assert report.labor_value_equivalent == (hours * D("17.56")).quantize(D("0.01"), decimal.ROUND_HALF_UP)
        assert report.recurring_cost == cost.quantize(D("0.01"), decimal.ROUND_HALF_UP)
        assert report.net_operational_value == report.labor_value_equivalent - report.recurring_cost
        assert report.recurring_cost_per_item == (cost / D(3140)).quantize(D("0.0001"), decimal.ROUND_HALF_UP)
    assert DAYS_PER_YEAR == 365


def test_the_labor_value_is_made_from_the_exact_hours_not_the_rounded_ones():
    # 69.78 (the hours as reported) x 17.56 would be 1225.34: four cents too many.
    report = _report(**FULL)

    assert report.labor_value_equivalent == D("1225.30")
    assert (report.staff_time_equivalent_hours * D("17.56")).quantize(D("0.01")) == D("1225.34")


def test_it_does_not_matter_whose_rate_it_is(  ):
    inherited = calculate_efficiency(SEPTEMBER, SEPTEMBER_CHECKINS, _settings({"manual_items_per_hour": "45.0", "labor_rate": "17.56"}, recurring_annual_cost="8400.00"))
    overridden = calculate_efficiency(
        SEPTEMBER,
        SEPTEMBER_CHECKINS,
        _settings({"manual_items_per_hour": "1.0", "labor_rate": "999.00"}, manual_items_per_hour="45.0", labor_rate="17.56", recurring_annual_cost="8400.00"),
    )
    mixed = _settings({"manual_items_per_hour": "45.0", "labor_rate": "99.00"}, labor_rate="17.56", recurring_annual_cost="8400.00")

    assert _figures(inherited) == _figures(overridden) == _figures(calculate_efficiency(SEPTEMBER, SEPTEMBER_CHECKINS, mixed))
    assert (mixed.manual_items_per_hour.source, mixed.labor_rate.source) == ("organization", "sorter")
    assert inherited.labor_value_equivalent == D("1225.30")


def test_the_one_time_cost_changes_nothing():
    without, unknown_cost, zero, large = _report(**FULL), _report(**FULL, one_time_cost=None), _report(**FULL, one_time_cost="0.00"), _report(**FULL, one_time_cost="100000000.00")

    assert without == unknown_cost == zero == large
    assert "one_time_cost" not in USED_SETTINGS
    assert "one_time_cost" not in inspect.getsource(calculate_efficiency)


# =====================================================================================================================
# What is not known
# =====================================================================================================================

def test_with_no_manual_rate_there_is_no_time_value_or_net_and_the_cost_is_still_worked_out():
    report = _report(labor_rate="17.56", recurring_annual_cost="8400.00", in_service_date="2020-11-20")

    assert _text(report) == (None, None, "690.41", None, "0.2199")
    assert report.missing == ("manual_items_per_hour",)


def test_with_no_labor_rate_there_is_time_and_no_value_or_net():
    report = _report(manual_items_per_hour="45.0", recurring_annual_cost="8400.00", in_service_date="2020-11-20")

    assert _text(report) == ("69.78", None, "690.41", None, "0.2199")
    assert report.missing == ("labor_rate",)


def test_with_no_recurring_cost_there_is_time_and_value_and_no_cost_net_or_cost_per_item():
    report = _report(manual_items_per_hour="45.0", labor_rate="17.56", in_service_date="2020-11-20")

    assert _text(report) == ("69.78", "1225.30", None, None, None)
    assert report.missing == ("recurring_annual_cost",)


def test_with_no_in_service_date_every_day_of_the_range_is_taken_and_the_date_is_listed_as_missing():
    report = _report(manual_items_per_hour="45.0", labor_rate="17.56", recurring_annual_cost="8400.00")

    assert (report.in_service_days, report.in_service_checkin_count) == (30, 3140)
    assert _text(report) == ("69.78", "1225.30", "690.41", "534.89", "0.2199")
    assert report.missing == ("in_service_date",)


def test_with_no_settings_at_all_the_check_ins_are_still_counted_and_nothing_is_invented():
    report = calculate_efficiency(SEPTEMBER, SEPTEMBER_CHECKINS, resolve_efficiency_settings(OrganizationEfficiencySettings(), SorterEfficiencySettings()))

    assert report == EfficiencyReport(3140, 30, 3140, None, None, None, None, None, ("manual_items_per_hour", "labor_rate", "recurring_annual_cost", "in_service_date"))
    assert report.missing == USED_SETTINGS


def test_missing_is_always_in_the_one_order():
    assert _report(labor_rate="17.56").missing == ("manual_items_per_hour", "recurring_annual_cost", "in_service_date")
    assert _report(in_service_date="2020-11-20").missing == ("manual_items_per_hour", "labor_rate", "recurring_annual_cost")


def test_an_explicit_zero_recurring_cost_is_a_cost_of_zero_not_an_unknown_one():
    report = _report(**{**FULL, "recurring_annual_cost": "0.00"})

    assert _text(report) == ("69.78", "1225.30", "0.00", "1225.30", "0.0000")
    assert report.recurring_cost is not None
    assert report.missing == ()


# =====================================================================================================================
# In-service days
# =====================================================================================================================

JANUARY = _dates("2026-01-01", 30)
# 10 check-ins on each of the first 15 days, 20 on each of the last 15.
JANUARY_CHECKINS = [10] * 15 + [20] * 15


@pytest.mark.parametrize(
    ("in_service_date", "days", "checkins"),
    [
        ("2020-11-20", 30, 450),     # long before the range
        ("2025-12-31", 30, 450),     # the day before it
        ("2026-01-01", 30, 450),     # its first day
        ("2026-01-02", 29, 440),
        ("2026-01-16", 15, 300),     # midway: only the days from then on, and only their check-ins
        ("2026-01-30", 1, 20),       # its last day
        ("2026-01-31", 0, 0),        # the day after it
        ("2026-06-01", 0, 0),        # long after a historical range
    ],
)
def test_the_in_service_days_are_the_days_of_the_range_on_or_after_the_date_and_only_their_check_ins_count(in_service_date, days, checkins):
    report = _report(JANUARY, JANUARY_CHECKINS, **{**FULL, "in_service_date": in_service_date})

    assert (report.in_service_days, report.in_service_checkin_count) == (days, checkins)
    # Every check-in of the range is still reported, whatever part of it is counted.
    assert report.checkin_count == 450
    assert report.missing == ()


def test_a_range_that_starts_before_the_sorter_was_in_service_is_worked_out_from_the_day_it_was():
    crossing = _report(JANUARY, JANUARY_CHECKINS, **{**FULL, "in_service_date": "2026-01-16"})
    second_half_alone = _report(JANUARY[15:], JANUARY_CHECKINS[15:], **{**FULL, "in_service_date": "2026-01-16"})

    # 300 check-ins over 15 days: 300 / 45 = 6.67 h; x 17.56 = 117.07; 8400 / 365 * 15 = 345.21.
    assert _text(crossing) == ("6.67", "117.07", "345.21", "-228.14", "1.1507")
    assert _figures(crossing) == _figures(second_half_alone)
    # Not the whole range's check-ins against half the range's cost.
    assert crossing.staff_time_equivalent_hours != (D(450) / D(45)).quantize(D("0.01"))


def test_a_date_after_the_whole_range_counts_nothing_and_charges_nothing():
    report = _report(JANUARY, JANUARY_CHECKINS, **{**FULL, "in_service_date": "2026-02-01"})

    assert (report.checkin_count, report.in_service_days, report.in_service_checkin_count) == (450, 0, 0)
    assert _text(report) == ("0.00", "0.00", "0.00", "0.00", None)


def test_a_single_day_range():
    report = _report([date(2026, 9, 30)], [240], **FULL)

    # 240 / 45 = 5.33; x 17.56 = 93.65; 8400 / 365 = 23.01; 23.0136... / 240 = 0.0959.
    assert (report.in_service_days, report.checkin_count) == (1, 240)
    assert _text(report) == ("5.33", "93.65", "23.01", "70.64", "0.0959")


def test_a_day_of_a_leap_year_costs_what_any_other_day_does():
    february_2028 = _dates("2028-02-01", 29)

    report = _report(february_2028, [0] * 29, **FULL)

    assert report.recurring_cost == (D("8400.00") * 29 / 365).quantize(D("0.01"), decimal.ROUND_HALF_UP) == D("667.40")


# =====================================================================================================================
# Little or nothing processed
# =====================================================================================================================

def test_with_no_check_ins_time_and_value_are_zero_the_cost_still_applies_and_there_is_no_cost_per_item():
    report = _report(SEPTEMBER, [0] * 30, **FULL)

    assert _text(report) == ("0.00", "0.00", "690.41", "-690.41", None)
    assert report.net_operational_value < 0
    assert (report.checkin_count, report.missing) == (0, ())


def test_no_check_ins_and_no_settings_is_nothing_at_all_and_still_not_an_error():
    assert _text(_report(SEPTEMBER, [0] * 30)) == (None, None, None, None, None)
    assert _text(_report(SEPTEMBER, [0] * 30, manual_items_per_hour="45.0")) == ("0.00", None, None, None, None)


def test_one_check_in():
    report = _report(SEPTEMBER, [1] + [0] * 29, **FULL)

    # 1 / 45 = 0.0222 h; x 17.56 = 0.3902; the whole month's cost falls on the one item.
    assert _text(report) == ("0.02", "0.39", "690.41", "-690.02", "690.4110")


def test_a_net_value_can_be_negative_and_is_exactly_the_difference_of_the_two_reported_figures():
    for checkins in (0, 1, 7, 45, 1000, 3140, 99999):
        for cost in ("0.00", "0.01", "365.00", "8400.00", "10000000.00"):
            report = _report(SEPTEMBER, [checkins] + [0] * 29, **{**FULL, "recurring_annual_cost": cost})
            assert report.net_operational_value == report.labor_value_equivalent - report.recurring_cost
            assert report.net_operational_value.as_tuple().exponent == -2
    assert str(_report(SEPTEMBER, [1000] + [0] * 29, **FULL).net_operational_value) == "-300.19"  # 390.22 - 690.41


# =====================================================================================================================
# Decimal, and rounding
# =====================================================================================================================

ONE_DAY = [date(2026, 9, 1)]


@pytest.mark.parametrize(
    ("checkins", "manual", "expected"),
    [
        (1, "200.0", "0.01"),     # exactly 0.005: half goes UP (half-even would give 0.00)
        (3, "200.0", "0.02"),     # exactly 0.015: up (half-even would give 0.02 too)
        (5, "200.0", "0.03"),     # exactly 0.025: up (half-even would give 0.02)
        (1, "204.1", "0.00"),     # 0.00489...: down
        (1, "199.9", "0.01"),     # 0.005002...: up
        (2, "3.0", "0.67"),       # 0.666...: repeating
        (1, "3.0", "0.33"),       # 0.333...: repeating
        (1, "7.0", "0.14"),       # 0.142857...
        (1000, "1000.0", "1.00"),
    ],
)
def test_hours_are_rounded_once_half_up_to_two_places(checkins, manual, expected):
    assert str(_report(ONE_DAY, [checkins], manual_items_per_hour=manual).staff_time_equivalent_hours) == expected


@pytest.mark.parametrize(
    ("checkins", "manual", "rate", "expected"),
    [
        (1, "200.0", "1.00", "0.01"),      # exactly 0.005
        (5, "200.0", "1.00", "0.03"),      # exactly 0.025: half-even would give 0.02
        (1, "3.0", "0.01", "0.00"),        # 0.00333...
        (1, "3.0", "1000.00", "333.33"),   # 333.333...
        (2, "3.0", "1000.00", "666.67"),   # 666.666...
        (1, "7.0", "17.56", "2.51"),       # 2.50857...
    ],
)
def test_labor_value_is_rounded_once_half_up_to_two_places(checkins, manual, rate, expected):
    assert str(_report(ONE_DAY, [checkins], manual_items_per_hour=manual, labor_rate=rate).labor_value_equivalent) == expected


@pytest.mark.parametrize(
    ("annual", "days", "expected"),
    [
        ("365.00", 1, "1.00"),
        ("365.00", 92, "92.00"),
        ("1.00", 1, "0.00"),         # 0.00273...
        ("1.83", 1, "0.01"),         # 0.005013...: up
        ("1.82", 1, "0.00"),         # 0.004986...: down
        ("3.65", 1, "0.01"),         # exactly 0.01
        ("1.46", 5, "0.02"),         # exactly 0.02
        ("0.73", 5, "0.01"),         # exactly 0.01
        ("8400.00", 30, "690.41"),
        ("8400.00", 5, "115.07"),
        ("0.01", 92, "0.00"),
    ],
)
def test_the_recurring_cost_is_the_yearly_cost_by_the_day_rounded_once(annual, days, expected):
    report = _report(_dates("2026-06-01", days), [0] * days, recurring_annual_cost=annual)

    assert str(report.recurring_cost) == expected


def test_the_recurring_cost_rounds_an_exact_half_cent_up():
    # 73.00 / 365 = 0.20 a day exactly; 0.365 a year is not a value the settings allow, so the half comes from days:
    # 18.25 / 365 = 0.05 a day; over 1 day 0.05. A half cent needs 1.825 a year -- not storable -- so check the rule
    # where the settings can reach it: 36.50 / 365 = 0.10; 3.65 * 1/365 = 0.01. The quantize itself is half up:
    assert efficiency_report._rounded(D("0.005"), D("0.01")) == D("0.01")
    assert efficiency_report._rounded(D("0.025"), D("0.01")) == D("0.03")
    assert efficiency_report._rounded(D("-0.005"), D("0.01")) == D("-0.01")
    assert efficiency_report._rounded(D("0.00005"), D("0.0001")) == D("0.0001")


@pytest.mark.parametrize(
    ("annual", "checkins", "expected"),
    [
        ("365.00", 8, "0.1250"),
        ("365.00", 20000, "0.0001"),     # exactly 0.00005: half up (half-even would give 0.0000)
        ("365.00", 20001, "0.0000"),     # just under
        ("365.00", 3, "0.3333"),         # repeating
        ("365.00", 6, "0.1667"),
        ("365.00", 1, "1.0000"),
        # Made from the EXACT cost, not the reported one: 1.00 a year is 0.0027 a day, reported as a cost of 0.00.
        ("1.00", 1, "0.0027"),
    ],
)
def test_the_cost_per_item_is_the_exact_cost_over_the_check_ins_rounded_once_to_four_places(annual, checkins, expected):
    report = _report(ONE_DAY, [checkins], recurring_annual_cost=annual)

    assert str(report.recurring_cost_per_item) == expected


def test_every_figure_has_exactly_the_places_it_is_reported_in():
    for checkins in (0, 1, 45, 90, 3140):
        report = _report(ONE_DAY, [checkins], **FULL)
        assert [figure.as_tuple().exponent for figure in _figures(report)[:4]] == [-2, -2, -2, -2]
        if checkins:
            assert report.recurring_cost_per_item.as_tuple().exponent == -4
    # A whole number of hours is still written to two places.
    assert str(_report(ONE_DAY, [90], manual_items_per_hour="45.0").staff_time_equivalent_hours) == "2.00"


def test_the_largest_values_the_settings_allow_are_worked_out_exactly():
    quarter = _dates("2026-01-01", 92)

    # A billion check-ins a day for 92 days, by hand at one item an hour, at the highest labor rate and yearly cost.
    report = _report(quarter, [10**9] * 92, manual_items_per_hour="1.0", labor_rate="1000.00", recurring_annual_cost="10000000.00")

    assert report.checkin_count == 92 * 10**9
    assert str(report.staff_time_equivalent_hours) == "92000000000.00"
    assert str(report.labor_value_equivalent) == "92000000000000.00"
    assert str(report.recurring_cost) == "2520547.95"       # 10,000,000 * 92 / 365 = 2,520,547.9452...
    assert str(report.net_operational_value) == "91999997479452.05"
    assert str(report.recurring_cost_per_item) == "0.0000"


def test_the_smallest_values_the_settings_allow():
    report = _report(ONE_DAY, [1], manual_items_per_hour="1000.0", labor_rate="0.01", recurring_annual_cost="0.01")

    assert _text(report) == ("0.00", "0.00", "0.00", "0.00", "0.0000")


def test_the_answer_does_not_depend_on_the_process_decimal_context():
    settings = _settings(**FULL)
    expected = calculate_efficiency(SEPTEMBER, SEPTEMBER_CHECKINS, settings)

    with decimal.localcontext() as context:
        context.prec = 3
        context.rounding = decimal.ROUND_DOWN
        assert calculate_efficiency(SEPTEMBER, SEPTEMBER_CHECKINS, settings) == expected


def test_nothing_is_worked_out_in_binary_floating_point():
    source = inspect.getsource(efficiency_report).split('"""', 2)[2]

    for forbidden in ("float(", "round(", "math.", " / ", " * ", "** "):
        assert forbidden not in source, forbidden
    for figure in _figures(_report(**FULL)):
        assert type(figure) is Decimal
    # 0.1 + 0.2: three check-ins by hand at ten an hour is exactly 0.30 hours.
    assert _report(ONE_DAY, [3], manual_items_per_hour="10.0").staff_time_equivalent_hours == D("0.30")


# =====================================================================================================================
# What it is given
# =====================================================================================================================

def test_the_counts_must_line_up_with_the_dates():
    with pytest.raises(ValueError, match="one check-in count for each date"):
        calculate_efficiency(SEPTEMBER, SEPTEMBER_CHECKINS[:-1], _settings(**FULL))
    with pytest.raises(ValueError, match="one check-in count for each date"):
        calculate_efficiency(SEPTEMBER[:-1], SEPTEMBER_CHECKINS, _settings(**FULL))


@pytest.mark.parametrize("bad", [-1, 1.0, 1.5, "1", None, True, D("1")])
def test_a_count_that_is_not_a_count_is_refused(bad):
    with pytest.raises(ValueError, match="whole number"):
        calculate_efficiency(ONE_DAY, [bad], _settings(**FULL))


def test_an_empty_range_is_nothing_counted_and_nothing_charged():
    report = calculate_efficiency([], [], _settings(**FULL))

    assert (report.checkin_count, report.in_service_days) == (0, 0)
    assert _text(report) == ("0.00", "0.00", "0.00", "0.00", None)


def test_it_accepts_the_tuples_the_report_service_gives_and_changes_nothing_it_is_given():
    dates, checkins = tuple(SEPTEMBER), tuple(SEPTEMBER_CHECKINS)
    settings = _settings(**FULL)

    assert calculate_efficiency(dates, checkins, settings) == _report(**FULL)
    assert (dates, checkins) == (tuple(SEPTEMBER), tuple(SEPTEMBER_CHECKINS))
    assert settings == _settings(**FULL)


# =====================================================================================================================
# The module itself
# =====================================================================================================================

def test_the_result_is_immutable_and_has_exactly_these_fields():
    assert [field.name for field in dataclasses.fields(EfficiencyReport)] == [
        "checkin_count",
        "in_service_days",
        "in_service_checkin_count",
        "staff_time_equivalent_hours",
        "labor_value_equivalent",
        "recurring_cost",
        "net_operational_value",
        "recurring_cost_per_item",
        "missing",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _report(**FULL).checkin_count = 1  # type: ignore[misc]


def test_there_is_no_projection_payback_return_or_machine_rate_here():
    code = inspect.getsource(efficiency_report).split('"""', 2)[2].lower()

    for forbidden in ("annualiz", "payback", "roi", "break_even", "since_install", "amh", "hours_saved", "one_time_cost", "365.25", "30.44"):
        assert forbidden not in code.replace("one_time_cost is not one", ""), forbidden


def test_the_module_is_pure():
    source = inspect.getsource(efficiency_report)

    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == [
        "from __future__ import annotations",
        "from collections.abc import Sequence",
        "from dataclasses import dataclass",
        "from datetime import date",
        "from decimal import ROUND_HALF_UP, Context, Decimal",
        "from services.efficiency_settings import EffectiveEfficiencySettings",
    ]
    for forbidden in ("date.today", ".now(", "getenv", "sqlalchemy", "fastapi", "streamlit"):
        assert forbidden not in source.split('"""', 2)[2], forbidden
