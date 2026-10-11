"""Reports R10D: SortView Value -- the settings model and the pure arithmetic.

    parse_sortview_value_settings / validate_sortview_value_settings / serialize_sortview_value_settings
    resolve_labor_rate
    calculate_sortview_value

Nothing here touches a database, a clock or a request: there is no route, no
service and no stored row yet. What is tested is what a block says, what is
refused, and what is worked out from it.

Imported the "flat" way (services.sortview_value), the identity the API
process uses.
"""

from __future__ import annotations

import dataclasses
import decimal
import inspect
import json
from datetime import date
from decimal import Decimal

import pytest

from services import sortview_value
from services.efficiency_settings import (
    OrganizationEfficiencySettings,
    SorterEfficiencySettings,
)
from services.sortview_value import (
    BASES,
    NOT_A_BASIS,
    REPORTING,
    SORTVIEW_VALUE_SETTINGS_KEY,
    TROUBLESHOOTING,
    USED_INPUTS,
    CategoryValue,
    ReportingInputs,
    ResolvedLaborRate,
    SortViewValueError,
    SortViewValueReport,
    SortViewValueSettings,
    TroubleshootingInputs,
    calculate_sortview_value,
    parse_sortview_value_settings,
    resolve_labor_rate,
    serialize_sortview_value_settings,
    validate_sortview_value_settings,
)

D = Decimal

# The product's date a write is validated against. Always passed in as today=: nothing under test reads a clock.
AS_OF = date(2026, 10, 6)

REPORTING_BLOCK = {
    "tasks_per_month": "12.0",
    "baseline_minutes_per_task": "90.0",
    "current_minutes_per_task": "15.0",
    "basis": "estimate",
    "as_of": "2026-09-01",
}
TROUBLESHOOTING_BLOCK = {
    "incidents_per_month": "4.0",
    "staff_per_incident": "2.0",
    "baseline_minutes": "45.0",
    "current_minutes": "20.0",
    "basis": "measured",
    "as_of": "2026-09-15",
}
# Reporting: 12 tasks, 90 minutes each before and 15 now -> 18.00 h and 3.00 h a month, 15.00 recovered.
# Troubleshooting: 4 incidents of 2 staff, 45 minutes each before and 20 now -> 6.00 h and 2.666... h, 3.333... recovered.
# At 32.50 an hour: 487.50 and 108.333... a month; 595.8333... together, 7,150.00 a year, against 4,800.00.
BLOCK = {
    "labor_rate": "32.50",
    "subscription_annual_cost": "4800.00",
    "reporting": REPORTING_BLOCK,
    "troubleshooting": TROUBLESHOOTING_BLOCK,
}
DOCUMENT = {
    "library_name": "Example Library",
    "security": {"admin_enabled": True, "admin_password_hash": "not-a-real-hash"},
    "efficiency": {"labor_rate": "20.00", "manual_items_per_hour": "45.0"},
    "sortview_value": BLOCK,
}

NO_EFFICIENCY = OrganizationEfficiencySettings()

DECIMAL_FIELDS = [
    "labor_rate",
    "subscription_annual_cost",
    "reporting.tasks_per_month",
    "reporting.baseline_minutes_per_task",
    "reporting.current_minutes_per_task",
    "troubleshooting.incidents_per_month",
    "troubleshooting.staff_per_incident",
    "troubleshooting.baseline_minutes",
    "troubleshooting.current_minutes",
]


def _block(**changes) -> dict:
    """BLOCK with fields changed by dotted name: `_block(**{"reporting.tasks_per_month": "3"})`. A value of ... removes
    the field."""
    block = {**BLOCK, "reporting": dict(REPORTING_BLOCK), "troubleshooting": dict(TROUBLESHOOTING_BLOCK)}
    for name, value in changes.items():
        target, _, field = name.rpartition(".")
        owner = block[target] if target else block
        if value is ...:
            owner.pop(field, None)
        else:
            owner[field] = value
    return block


def _only(name: str, value: object) -> dict:
    """A block holding the one field `name`, by dotted name."""
    category, _, field = name.rpartition(".")
    return {category: {field: value}} if category else {field: value}


def _field(settings: SortViewValueSettings, name: str):
    category, _, field = name.rpartition(".")
    return getattr(getattr(settings, category) if category else settings, field)


def _parse(block: object) -> SortViewValueSettings:
    return parse_sortview_value_settings({SORTVIEW_VALUE_SETTINGS_KEY: block})


def _problems(error: pytest.ExceptionInfo) -> list[tuple[str, str]]:
    return [(problem.field, problem.code) for problem in error.value.problems]


def _refused(block: object) -> list[tuple[str, str]]:
    with pytest.raises(SortViewValueError) as error:
        validate_sortview_value_settings(block, today=AS_OF)
    return _problems(error)


def _report(block: dict | None = None, efficiency: OrganizationEfficiencySettings = NO_EFFICIENCY) -> SortViewValueReport:
    settings = _parse(BLOCK if block is None else block)
    return calculate_sortview_value(settings, resolve_labor_rate(settings, efficiency))


# =====================================================================================================================
# Reading a stored document
# =====================================================================================================================

def test_a_document_gives_every_field_as_decimals_dates_and_a_basis():
    settings = parse_sortview_value_settings(DOCUMENT)

    assert settings == SortViewValueSettings(
        labor_rate=D("32.50"),
        subscription_annual_cost=D("4800.00"),
        reporting=ReportingInputs(D("12.0"), D("90.0"), D("15.0"), "estimate", date(2026, 9, 1)),
        troubleshooting=TroubleshootingInputs(D("4.0"), D("2.0"), D("45.0"), D("20.0"), "measured", date(2026, 9, 15)),
    )
    assert isinstance(settings.labor_rate, Decimal)
    assert isinstance(settings.reporting.tasks_per_month, Decimal)


@pytest.mark.parametrize("document", [{}, {"library_name": "Example"}, {"sortview_value": None}, {"sortview_value": {}}, None, [], "text", 7])
def test_a_document_with_no_block_is_valid_and_says_nothing(document):
    assert parse_sortview_value_settings(document) == SortViewValueSettings()
    assert parse_sortview_value_settings(document).reporting == ReportingInputs()
    assert parse_sortview_value_settings(document).troubleshooting == TroubleshootingInputs()


@pytest.mark.parametrize("name", [*DECIMAL_FIELDS, "reporting.basis", "reporting.as_of", "troubleshooting.basis", "troubleshooting.as_of"])
def test_a_missing_or_null_field_is_unknown_and_the_rest_is_still_read(name):
    for block in (_block(**{name: ...}), _block(**{name: None})):
        settings = _parse(block)

        assert _field(settings, name) is None
        others = [other for other in DECIMAL_FIELDS if other != name]
        assert all(_field(settings, other) is not None for other in others)


@pytest.mark.parametrize("category", [REPORTING, TROUBLESHOOTING])
def test_a_missing_or_null_kind_of_work_is_all_unknown(category):
    for block in ({**BLOCK, category: None}, {key: value for key, value in BLOCK.items() if key != category}):
        settings = _parse(block)

        assert getattr(settings, category) in (ReportingInputs(), TroubleshootingInputs())
        assert settings.labor_rate == D("32.50")


def test_an_entered_zero_is_zero_and_is_not_the_same_as_unknown():
    zero = _parse(_block(**{"subscription_annual_cost": "0", "reporting.current_minutes_per_task": "0.0", "troubleshooting.incidents_per_month": "0"}))
    unknown = _parse(_block(**{"subscription_annual_cost": ..., "reporting.current_minutes_per_task": ..., "troubleshooting.incidents_per_month": ...}))

    assert zero.subscription_annual_cost == D("0.00") and zero.subscription_annual_cost is not None
    assert zero.reporting.current_minutes_per_task == D("0.0")
    assert zero.troubleshooting.incidents_per_month == D("0.0")
    assert unknown.subscription_annual_cost is None
    assert unknown.reporting.current_minutes_per_task is None
    assert unknown.troubleshooting.incidents_per_month is None
    assert zero != unknown


def test_there_is_no_built_in_rate_volume_or_duration_anywhere_in_the_module():
    assert parse_sortview_value_settings({}) == SortViewValueSettings(None, None, ReportingInputs(), TroubleshootingInputs())
    assert all(value is None for value in dataclasses.astuple(ReportingInputs()))
    assert all(value is None for value in dataclasses.astuple(TroubleshootingInputs()))
    source = inspect.getsource(sortview_value)
    for assumed in ("17.56", "18.0", "45.0", "30.44", "365"):
        assert assumed not in source, assumed


@pytest.mark.parametrize("block", ["32.50", ["labor_rate", "32.50"], 20, True, 0, "", []])
def test_a_block_that_is_not_an_object_is_refused(block):
    with pytest.raises(SortViewValueError) as error:
        _parse(block)

    assert _problems(error) == [("sortview_value", "not_an_object")]


@pytest.mark.parametrize("category", [REPORTING, TROUBLESHOOTING])
@pytest.mark.parametrize("value", ["12", ["tasks_per_month"], 12, True, 0, ""])
def test_a_kind_of_work_that_is_not_an_object_is_refused_by_its_name(category, value):
    with pytest.raises(SortViewValueError) as error:
        _parse({**BLOCK, category: value})

    assert _problems(error) == [(category, "not_an_object")]


def test_every_other_key_of_a_document_is_ignored_however_malformed():
    document = {**DOCUMENT, "efficiency": "not even an object", "transit": 7, "security": None}

    assert parse_sortview_value_settings(document) == parse_sortview_value_settings(DOCUMENT)


def test_reading_ignores_keys_it_does_not_know_in_the_block_and_in_each_kind_of_work():
    block = _block(**{"category_c": {"hours": "3"}, "reporting.notes": "from the 2025 time study", "troubleshooting.vendor_calls": "2"})

    assert _parse(block) == _parse(BLOCK)


def test_a_malformed_block_names_every_field_at_fault_in_field_order_and_quotes_no_value():
    block = {
        "labor_rate": "thirty-two",
        "subscription_annual_cost": 4800,
        "reporting": {"tasks_per_month": "-12", "baseline_minutes_per_task": "90.55", "current_minutes_per_task": "15.0", "basis": "guess", "as_of": "09/01/2026"},
        "troubleshooting": {"incidents_per_month": "1e3", "staff_per_incident": "0", "baseline_minutes": "10000.1", "current_minutes": None, "basis": 3},
    }

    with pytest.raises(SortViewValueError) as error:
        _parse(block)

    assert _problems(error) == [
        ("labor_rate", "not_a_decimal"),
        ("subscription_annual_cost", "not_a_string"),
        ("reporting.tasks_per_month", "not_a_decimal"),
        ("reporting.baseline_minutes_per_task", "too_many_decimal_places"),
        ("reporting.basis", "not_a_basis"),
        ("reporting.as_of", "not_a_date"),
        ("troubleshooting.incidents_per_month", "not_a_decimal"),
        ("troubleshooting.staff_per_incident", "out_of_range"),
        ("troubleshooting.baseline_minutes", "out_of_range"),
        ("troubleshooting.basis", "not_a_string"),
    ]
    message = str(error.value)
    assert message.startswith("Invalid SortView Value settings: labor_rate (not_a_decimal), ")
    for value in ("thirty-two", "4800", "-12", "90.55", "guess", "09/01", "1e3", "10000.1"):
        assert value not in message, value
    assert isinstance(error.value, ValueError)


def test_a_stored_as_of_date_is_not_checked_against_any_clock():
    settings = _parse(_block(**{"reporting.as_of": "2999-12-31"}))

    assert settings.reporting.as_of == date(2999, 12, 31)
    assert "today" not in inspect.signature(parse_sortview_value_settings).parameters


# =====================================================================================================================
# What a value may be
# =====================================================================================================================

@pytest.mark.parametrize(
    "text, expected",
    [("32.50", "32.50"), ("32.5", "32.50"), ("32", "32.00"), ("0.01", "0.01"), ("10000", "10000.00"), ("10000.00", "10000.00")],
)
def test_a_labor_rate_is_plain_decimal_text_kept_to_two_places(text, expected):
    rate = _parse({"labor_rate": text}).labor_rate

    assert rate == D(expected)
    assert str(rate) == expected


@pytest.mark.parametrize("name", DECIMAL_FIELDS)
@pytest.mark.parametrize(
    "value, code",
    [
        (32.5, "not_a_string"),
        (32, "not_a_string"),
        (True, "not_a_string"),
        (["32.50"], "not_a_string"),
        ({"amount": "32.50"}, "not_a_string"),
        ("", "not_a_decimal"),
        (" 32.50", "not_a_decimal"),
        ("32.50 ", "not_a_decimal"),
        ("+32.50", "not_a_decimal"),
        ("-32.50", "not_a_decimal"),
        ("$32.50", "not_a_decimal"),
        ("1,200", "not_a_decimal"),
        ("3e1", "not_a_decimal"),
        ("032.5", "not_a_decimal"),
        (".5", "not_a_decimal"),
        ("5.", "not_a_decimal"),
        ("NaN", "not_a_decimal"),
        ("Infinity", "not_a_decimal"),
        ("٣٢", "not_a_decimal"),
        ("1" * 33, "not_a_decimal"),
    ],
)
def test_a_figure_that_is_not_plain_decimal_text_is_refused_with_a_reason(name, value, code):
    assert _refused(_only(name, value)) == [(name, code)]
    with pytest.raises(SortViewValueError) as error:
        _parse(_only(name, value))
    assert _problems(error) == [(name, code)]


def test_a_json_number_is_refused_exactly_as_json_delivers_it():
    block = json.loads('{"labor_rate": 32.50, "reporting": {"tasks_per_month": 12}}')

    assert _refused(block) == [("labor_rate", "not_a_string"), ("reporting.tasks_per_month", "not_a_string")]


@pytest.mark.parametrize(
    "name, accepted, refused",
    [
        # A rate is above zero; a subscription can cost nothing.
        ("labor_rate", ["0.01", "10000", "10000.00"], ["0", "0.00", "10000.01", "10001"]),
        ("subscription_annual_cost", ["0", "0.00", "10000000", "10000000.00"], ["10000000.01", "10000001"]),
        ("reporting.tasks_per_month", ["0", "0.1", "100000", "100000.0"], ["100000.1", "100001"]),
        ("reporting.baseline_minutes_per_task", ["0", "0.1", "10000", "10000.0"], ["10000.1", "10001"]),
        ("reporting.current_minutes_per_task", ["0", "0.1", "10000", "10000.0"], ["10000.1", "10001"]),
        ("troubleshooting.incidents_per_month", ["0", "0.1", "100000", "100000.0"], ["100000.1", "100001"]),
        # An incident nobody attends is not an incident.
        ("troubleshooting.staff_per_incident", ["0.1", "1", "1000", "1000.0"], ["0", "0.0", "1000.1", "1001"]),
        ("troubleshooting.baseline_minutes", ["0", "0.1", "10000", "10000.0"], ["10000.1", "10001"]),
        ("troubleshooting.current_minutes", ["0", "0.1", "10000", "10000.0"], ["10000.1", "10001"]),
    ],
)
def test_each_field_has_its_own_bounds_both_ends_as_stated(name, accepted, refused):
    for value in accepted:
        assert _field(validate_sortview_value_settings(_only(name, value), today=AS_OF), name) == D(value)
        assert _field(_parse(_only(name, value)), name) == D(value)
    for value in refused:
        assert _refused(_only(name, value)) == [(name, "out_of_range")]


def test_the_bounds_are_the_generous_ones_agreed_and_no_field_was_left_out():
    limits = {name: (rule.minimum, rule.maximum, rule.above_minimum) for name, rule in sortview_value._FIELDS.items()}
    for category, fields in sortview_value._CATEGORY_FIELDS.items():
        limits.update({f"{category}.{name}": (rule.minimum, rule.maximum, rule.above_minimum) for name, rule in fields.items()})

    assert limits == {
        "labor_rate": (0, 10_000, True),
        "subscription_annual_cost": (0, 10_000_000, False),
        "reporting.tasks_per_month": (0, 100_000, False),
        "reporting.baseline_minutes_per_task": (0, 10_000, False),
        "reporting.current_minutes_per_task": (0, 10_000, False),
        "troubleshooting.incidents_per_month": (0, 100_000, False),
        "troubleshooting.staff_per_incident": (0, 1_000, True),
        "troubleshooting.baseline_minutes": (0, 10_000, False),
        "troubleshooting.current_minutes": (0, 10_000, False),
    }
    assert sorted(limits) == sorted(DECIMAL_FIELDS) == sorted(USED_INPUTS)


@pytest.mark.parametrize(
    "name, kept, too_many",
    [
        ("labor_rate", "32.55", "32.555"),
        ("subscription_annual_cost", "4800.99", "4800.999"),
        ("reporting.tasks_per_month", "2.5", "2.55"),
        ("reporting.baseline_minutes_per_task", "37.5", "37.55"),
        ("reporting.current_minutes_per_task", "7.5", "7.25"),
        ("troubleshooting.incidents_per_month", "2.5", "2.25"),
        ("troubleshooting.staff_per_incident", "1.5", "1.25"),
        ("troubleshooting.baseline_minutes", "37.5", "37.25"),
        ("troubleshooting.current_minutes", "12.5", "12.25"),
    ],
)
def test_no_field_takes_more_decimal_places_than_it_keeps_and_nothing_is_rounded_to_fit(name, kept, too_many):
    assert _field(_parse(_only(name, kept)), name) == D(kept)
    assert _refused(_only(name, too_many)) == [(name, "too_many_decimal_places")]


def test_counts_minutes_and_staff_may_all_be_fractions():
    settings = _parse(
        {
            "reporting": {"tasks_per_month": "2.5", "baseline_minutes_per_task": "37.5", "current_minutes_per_task": "7.5"},
            "troubleshooting": {"incidents_per_month": "0.5", "staff_per_incident": "1.5", "baseline_minutes": "22.5", "current_minutes": "0.1"},
        }
    )

    assert settings.reporting == ReportingInputs(D("2.5"), D("37.5"), D("7.5"))
    assert settings.troubleshooting == TroubleshootingInputs(D("0.5"), D("1.5"), D("22.5"), D("0.1"))


@pytest.mark.parametrize("category", [REPORTING, TROUBLESHOOTING])
def test_a_basis_is_one_of_two_words_and_nothing_else(category):
    assert BASES == ("estimate", "measured")
    for basis in BASES:
        assert getattr(_parse({category: {"basis": basis}}), category).basis == basis
    for value in ("Estimate", "MEASURED", "guess", "", " estimate", "estimated", "time study"):
        assert _refused({category: {"basis": value}}) == [(f"{category}.basis", NOT_A_BASIS)]
    for value in (1, True, ["estimate"], {"kind": "estimate"}):
        assert _refused({category: {"basis": value}}) == [(f"{category}.basis", "not_a_string")]
    assert NOT_A_BASIS == "not_a_basis"


@pytest.mark.parametrize("category", [REPORTING, TROUBLESHOOTING])
@pytest.mark.parametrize(
    "value, code",
    [
        ("2026-9-1", "not_a_date"),
        ("09/01/2026", "not_a_date"),
        ("2026-02-30", "not_a_date"),
        ("2026-09-01T00:00:00", "not_a_date"),
        ("", "not_a_date"),
        ("today", "not_a_date"),
        (20260901, "not_a_string"),
        (["2026-09-01"], "not_a_string"),
    ],
)
def test_an_as_of_date_is_a_calendar_date_written_out(category, value, code):
    assert _refused({category: {"as_of": value}}) == [(f"{category}.as_of", code)]


@pytest.mark.parametrize("category", [REPORTING, TROUBLESHOOTING])
def test_a_submitted_as_of_date_may_be_today_and_not_after_it(category):
    assert getattr(validate_sortview_value_settings({category: {"as_of": "2026-10-06"}}, today=AS_OF), category).as_of == AS_OF
    assert getattr(validate_sortview_value_settings({category: {"as_of": "2019-01-01"}}, today=AS_OF), category).as_of == date(2019, 1, 1)
    assert _refused({category: {"as_of": "2026-10-07"}}) == [(f"{category}.as_of", "in_the_future")]
    # The same date is fine a day later: today is whoever calls says it is.
    assert validate_sortview_value_settings({category: {"as_of": "2026-10-07"}}, today=date(2026, 10, 7)) is not None


# =====================================================================================================================
# What someone is about to store
# =====================================================================================================================

def test_a_valid_submitted_block_is_what_reading_it_gives():
    assert validate_sortview_value_settings(BLOCK, today=AS_OF) == _parse(BLOCK)


@pytest.mark.parametrize("block", [None, {}, {"labor_rate": None, "reporting": None, "troubleshooting": {}}])
def test_a_submitted_block_with_nothing_set_is_valid_and_sets_nothing(block):
    assert validate_sortview_value_settings(block, today=AS_OF) == SortViewValueSettings()


def test_a_submitted_key_that_is_not_a_field_is_refused_wherever_it_is():
    block = _block(**{"category_c": {"hours": "3"}, "efficiency": {}, "reporting.notes": "x", "troubleshooting.vendor_calls": "2"})

    assert _refused(block) == [
        ("reporting.notes", "unknown_field"),
        ("troubleshooting.vendor_calls", "unknown_field"),
        ("category_c", "unknown_field"),
        ("efficiency", "unknown_field"),
    ]


def test_a_field_of_one_kind_of_work_is_not_a_field_of_the_other_or_of_the_block():
    assert _refused({"reporting": {"incidents_per_month": "4"}}) == [("reporting.incidents_per_month", "unknown_field")]
    assert _refused({"troubleshooting": {"tasks_per_month": "4"}}) == [("troubleshooting.tasks_per_month", "unknown_field")]
    assert _refused({"tasks_per_month": "4", "basis": "estimate"}) == [("tasks_per_month", "unknown_field"), ("basis", "unknown_field")]
    assert _refused({"reporting": {"labor_rate": "20.00"}}) == [("reporting.labor_rate", "unknown_field")]


def test_a_submitted_block_reports_bad_values_and_unknown_keys_together_and_quotes_neither_value():
    with pytest.raises(SortViewValueError) as error:
        validate_sortview_value_settings({"labor_rate": "-7.77", "secret": "p4ssw0rd", "reporting": {"tasks_per_month": "99x"}}, today=AS_OF)

    assert _problems(error) == [("labor_rate", "not_a_decimal"), ("reporting.tasks_per_month", "not_a_decimal"), ("secret", "unknown_field")]
    assert "-7.77" not in str(error.value) and "p4ssw0rd" not in str(error.value) and "99x" not in str(error.value)


@pytest.mark.parametrize("block", ["32.50", [], 7, True, ""])
def test_a_submitted_block_that_is_not_an_object_is_refused(block):
    assert _refused(block) == [("sortview_value", "not_an_object")]


def test_nothing_is_cross_checked_work_may_take_longer_now_than_it_did():
    block = _block(**{"reporting.baseline_minutes_per_task": "15", "reporting.current_minutes_per_task": "90"})

    assert validate_sortview_value_settings(block, today=AS_OF).reporting.current_minutes_per_task == D("90.0")


# =====================================================================================================================
# What is stored
# =====================================================================================================================

def test_serializing_writes_canonical_text_each_kind_of_work_under_its_own_key():
    loose = {
        "labor_rate": "32.5",
        "subscription_annual_cost": "4800",
        "reporting": {"tasks_per_month": "12", "baseline_minutes_per_task": "90", "current_minutes_per_task": "15", "basis": "estimate", "as_of": "2026-09-01"},
        "troubleshooting": {"incidents_per_month": "4", "staff_per_incident": "2", "baseline_minutes": "45", "current_minutes": "20", "basis": "measured", "as_of": "2026-09-15"},
    }

    assert serialize_sortview_value_settings(_parse(loose)) == BLOCK
    assert list(serialize_sortview_value_settings(_parse(loose))) == ["labor_rate", "subscription_annual_cost", "reporting", "troubleshooting"]
    assert list(serialize_sortview_value_settings(_parse(loose))["reporting"]) == list(REPORTING_BLOCK)
    assert list(serialize_sortview_value_settings(_parse(loose))["troubleshooting"]) == list(TROUBLESHOOTING_BLOCK)


def test_serializing_leaves_out_what_is_unknown_and_writes_out_a_zero():
    settings = SortViewValueSettings(
        subscription_annual_cost=D("0"),
        reporting=ReportingInputs(tasks_per_month=D("0"), current_minutes_per_task=D("0.0")),
    )

    assert serialize_sortview_value_settings(settings) == {
        "subscription_annual_cost": "0.00",
        "reporting": {"tasks_per_month": "0.0", "current_minutes_per_task": "0.0"},
    }


def test_nothing_configured_has_one_stored_form_an_empty_block():
    assert serialize_sortview_value_settings(SortViewValueSettings()) == {}
    assert serialize_sortview_value_settings(_parse({"reporting": {}, "troubleshooting": None})) == {}


def test_a_kind_of_work_with_only_its_basis_or_date_is_still_stored():
    settings = _parse({"troubleshooting": {"basis": "measured"}, "reporting": {"as_of": "2026-09-01"}})

    assert serialize_sortview_value_settings(settings) == {"reporting": {"as_of": "2026-09-01"}, "troubleshooting": {"basis": "measured"}}


def test_serializing_never_writes_an_exponent_or_drops_a_place():
    settings = SortViewValueSettings(
        labor_rate=D("1E+3"),
        subscription_annual_cost=D("1E+7"),
        reporting=ReportingInputs(tasks_per_month=D("1E+5"), baseline_minutes_per_task=D("5")),
    )

    assert serialize_sortview_value_settings(settings) == {
        "labor_rate": "1000.00",
        "subscription_annual_cost": "10000000.00",
        "reporting": {"tasks_per_month": "100000.0", "baseline_minutes_per_task": "5.0"},
    }


@pytest.mark.parametrize(
    "block",
    [
        BLOCK,
        {},
        {"labor_rate": "0.01"},
        {"subscription_annual_cost": "0.00", "reporting": {"tasks_per_month": "0.0"}},
        {"troubleshooting": {"incidents_per_month": "2.5", "staff_per_incident": "1.5", "basis": "estimate"}},
        {"reporting": {"basis": "measured", "as_of": "2020-02-29"}},
    ],
)
def test_what_is_serialized_reads_back_as_the_same_settings_and_the_same_text(block):
    settings = _parse(block)
    stored = serialize_sortview_value_settings(settings)

    assert stored == block
    assert _parse(stored) == settings
    assert validate_sortview_value_settings(stored, today=AS_OF) == settings
    # Through JSON, as it is stored: text all the way, never a number.
    assert _parse(json.loads(json.dumps(stored))) == settings
    assert serialize_sortview_value_settings(_parse(json.loads(json.dumps(stored)))) == stored
    assert all(isinstance(value, (str, dict)) for value in stored.values())


def test_serializing_does_not_change_the_figures_worked_out():
    again = serialize_sortview_value_settings(_parse(BLOCK))

    assert _report(again) == _report(BLOCK)


# =====================================================================================================================
# The labor rate
# =====================================================================================================================

def test_the_blocks_own_labor_rate_applies_and_says_it_is_its_own():
    settings = _parse({"labor_rate": "32.50"})

    assert resolve_labor_rate(settings, NO_EFFICIENCY) == ResolvedLaborRate(D("32.50"), "sortview_value")
    assert resolve_labor_rate(settings, OrganizationEfficiencySettings(labor_rate=D("20.00"))) == ResolvedLaborRate(D("32.50"), "sortview_value")


def test_without_its_own_the_organizations_efficiency_default_applies_and_says_so():
    efficiency = OrganizationEfficiencySettings(labor_rate=D("20.00"), manual_items_per_hour=D("45.0"))

    assert resolve_labor_rate(_parse({}), efficiency) == ResolvedLaborRate(D("20.00"), "organization_efficiency")
    assert resolve_labor_rate(_parse({"subscription_annual_cost": "4800"}), efficiency).source == "organization_efficiency"


def test_with_neither_the_rate_is_unknown_and_no_rate_is_made_up():
    assert resolve_labor_rate(_parse({}), NO_EFFICIENCY) is None
    assert resolve_labor_rate(_parse(BLOCK | {"labor_rate": None}), OrganizationEfficiencySettings(manual_items_per_hour=D("45.0"))) is None


def test_only_the_organizations_default_is_ever_borrowed_never_a_sorters_override():
    parameters = inspect.signature(resolve_labor_rate).parameters

    assert list(parameters) == ["settings", "organization_efficiency"]
    assert "Sorter" not in inspect.getsource(sortview_value)
    assert not hasattr(SorterEfficiencySettings(labor_rate=D("99.00")), "source")


def test_the_same_rate_is_used_for_both_kinds_of_work_whichever_it_came_from():
    own = _report(BLOCK)
    borrowed = _report(_block(labor_rate=...), OrganizationEfficiencySettings(labor_rate=D("32.50")))

    assert own == borrowed
    assert not any("rate" in field.name for field in dataclasses.fields(ReportingInputs) + dataclasses.fields(TroubleshootingInputs))


# =====================================================================================================================
# The estimate
# =====================================================================================================================

def test_reporting_is_tasks_times_minutes_over_sixty_before_and_now():
    report = _report()

    # 12 * 90 / 60 = 18; 12 * 15 / 60 = 3; 15 recovered; * 32.50 = 487.50; * 12 = 5,850.00.
    assert report.reporting == CategoryValue(D("18.00"), D("3.00"), D("15.00"), D("487.50"), D("5850.00"))


def test_troubleshooting_is_incidents_times_staff_times_minutes_over_sixty_before_and_now():
    report = _report()

    # 4 * 2 * 45 / 60 = 6; 4 * 2 * 20 / 60 = 2.666...; 3.333... recovered; * 32.50 = 108.333...; * 12 = 1,300.00 exactly.
    assert report.troubleshooting == CategoryValue(D("6.00"), D("2.67"), D("3.33"), D("108.33"), D("1300.00"))


def test_the_organizations_figures_are_both_kinds_of_work_together_against_the_subscription():
    report = _report()

    assert report.recovered_hours_per_month == D("18.33")       # 15 + 3.333...
    assert report.monthly_value == D("595.83")                  # 487.50 + 108.333...
    assert report.annual_value == D("7150.00")                  # 5,850 + 1,300
    assert report.net_annual_value == D("2350.00")              # 7,150 - 4,800
    assert report.roi_percent == D("49.0")                      # 2,350 / 4,800 = 48.958...%
    assert report.payback_months == D("8.1")                    # 4,800 / 595.8333... = 8.0559...
    assert report.missing == ()


def test_fractions_are_worked_through_exactly():
    report = _report(
        {
            "labor_rate": "27.75",
            "subscription_annual_cost": "1500.00",
            "reporting": {"tasks_per_month": "2.5", "baseline_minutes_per_task": "37.5", "current_minutes_per_task": "7.5"},
            "troubleshooting": {"incidents_per_month": "0.5", "staff_per_incident": "1.5", "baseline_minutes": "22.5", "current_minutes": "2.5"},
        }
    )

    # Reporting: 2.5 * 37.5 / 60 = 1.5625; 2.5 * 7.5 / 60 = 0.3125; 1.25 recovered; * 27.75 = 34.6875.
    assert report.reporting == CategoryValue(D("1.56"), D("0.31"), D("1.25"), D("34.69"), D("416.25"))
    # Troubleshooting: 0.75 * 22.5 / 60 = 0.28125; 0.75 * 2.5 / 60 = 0.03125; 0.25 recovered; * 27.75 = 6.9375.
    assert report.troubleshooting == CategoryValue(D("0.28"), D("0.03"), D("0.25"), D("6.94"), D("83.25"))
    assert report.recovered_hours_per_month == D("1.50")
    assert report.monthly_value == D("41.63")                   # 41.625, half up
    assert report.annual_value == D("499.50")
    assert report.net_annual_value == D("-1000.50")
    assert report.roi_percent == D("-66.7")
    assert report.payback_months == D("36.0")                   # 1,500 / 41.625 = 36.036...


def test_payback_is_the_months_of_monthly_value_it_takes_to_cover_a_year_of_the_subscription():
    # 100.00 a month against 1,200.00 a year: twelve months of it.
    block = {
        "labor_rate": "50.00",
        "subscription_annual_cost": "1200.00",
        "reporting": {"tasks_per_month": "1", "baseline_minutes_per_task": "60", "current_minutes_per_task": "0"},
        "troubleshooting": {"incidents_per_month": "1", "staff_per_incident": "1", "baseline_minutes": "60", "current_minutes": "0"},
    }

    assert _report(block).monthly_value == D("100.00")
    assert _report(block).payback_months == D("12.0")
    assert _report(block | {"subscription_annual_cost": "600.00"}).payback_months == D("6.0")
    assert _report(block | {"subscription_annual_cost": "2400.00"}).payback_months == D("24.0")
    assert "months of the estimated monthly value" in " ".join(sortview_value.__doc__.split())


# --- what is not known -----------------------------------------------------------------------------------------------

REPORTING_NONE = CategoryValue(None, None, None, None, None)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("reporting.tasks_per_month", REPORTING_NONE),
        # The side whose own inputs are there is still worked out; the difference, and its worth, are not.
        ("reporting.baseline_minutes_per_task", CategoryValue(None, D("3.00"), None, None, None)),
        ("reporting.current_minutes_per_task", CategoryValue(D("18.00"), None, None, None, None)),
    ],
)
def test_incomplete_reporting_has_no_reporting_value_and_there_are_no_organization_figures(name, expected):
    report = _report(_block(**{name: ...}))

    assert report.reporting == expected
    # The other kind of work is whole, and is still reported by itself.
    assert report.troubleshooting == CategoryValue(D("6.00"), D("2.67"), D("3.33"), D("108.33"), D("1300.00"))
    # But it is never passed off as the whole.
    assert (report.recovered_hours_per_month, report.monthly_value, report.annual_value) == (None, None, None)
    assert (report.net_annual_value, report.roi_percent, report.payback_months) == (None, None, None)
    assert report.missing == (name,)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("troubleshooting.incidents_per_month", REPORTING_NONE),
        ("troubleshooting.staff_per_incident", REPORTING_NONE),
        ("troubleshooting.baseline_minutes", CategoryValue(None, D("2.67"), None, None, None)),
        ("troubleshooting.current_minutes", CategoryValue(D("6.00"), None, None, None, None)),
    ],
)
def test_incomplete_troubleshooting_has_no_troubleshooting_value_and_there_are_no_organization_figures(name, expected):
    report = _report(_block(**{name: ...}))

    assert report.troubleshooting == expected
    assert report.reporting == CategoryValue(D("18.00"), D("3.00"), D("15.00"), D("487.50"), D("5850.00"))
    assert (report.recovered_hours_per_month, report.monthly_value, report.annual_value) == (None, None, None)
    assert (report.net_annual_value, report.roi_percent, report.payback_months) == (None, None, None)
    assert report.missing == (name,)


@pytest.mark.parametrize("category", [REPORTING, TROUBLESHOOTING])
def test_one_kind_of_work_alone_is_never_summed_into_a_total(category):
    report = _report({key: value for key, value in BLOCK.items() if key != category})
    present = report.troubleshooting if category == REPORTING else report.reporting

    assert getattr(report, category) == REPORTING_NONE
    assert present.annual_value is not None
    for total in ("recovered_hours_per_month", "monthly_value", "annual_value", "net_annual_value", "roi_percent", "payback_months"):
        assert getattr(report, total) is None, total
    assert report.missing == tuple(name for name in USED_INPUTS if name.startswith(f"{category}."))


def test_with_no_labor_rate_the_hours_are_known_and_nothing_has_a_value():
    report = _report(_block(labor_rate=...))

    assert report.reporting == CategoryValue(D("18.00"), D("3.00"), D("15.00"), None, None)
    assert report.troubleshooting == CategoryValue(D("6.00"), D("2.67"), D("3.33"), None, None)
    assert report.recovered_hours_per_month == D("18.33")
    assert (report.monthly_value, report.annual_value, report.net_annual_value, report.roi_percent, report.payback_months) == (None,) * 5
    assert report.missing == ("labor_rate",)


def test_with_no_subscription_cost_the_value_is_known_and_nothing_is_set_against_it():
    report = _report(_block(subscription_annual_cost=...))

    assert report.monthly_value == D("595.83")
    assert report.annual_value == D("7150.00")
    assert (report.net_annual_value, report.roi_percent, report.payback_months) == (None, None, None)
    assert report.missing == ("subscription_annual_cost",)


def test_a_subscription_that_costs_nothing_has_a_net_value_and_no_return_on_it():
    report = _report(_block(subscription_annual_cost="0"))

    assert report.net_annual_value == D("7150.00")
    # A return on a cost of zero does not exist: not 0%, not infinite.
    assert report.roi_percent is None
    # Nothing to cover: no months of value are needed.
    assert report.payback_months == D("0.0")
    assert report.missing == ()


def test_an_entered_zero_is_worked_through_as_zero_and_is_never_missing():
    block = _block(**{"reporting.tasks_per_month": "0", "troubleshooting.current_minutes": "0"})
    report = _report(block)

    assert report.reporting == CategoryValue(D("0.00"), D("0.00"), D("0.00"), D("0.00"), D("0.00"))
    assert report.troubleshooting == CategoryValue(D("6.00"), D("0.00"), D("6.00"), D("195.00"), D("2340.00"))
    assert report.monthly_value == D("195.00")
    assert report.missing == ()
    # The same fields left out are unknown: no figure, and named.
    unknown = _report(_block(**{"reporting.tasks_per_month": ..., "troubleshooting.current_minutes": ...}))
    assert unknown.reporting.recovered_hours_per_month is None and unknown.troubleshooting.recovered_hours_per_month is None
    assert unknown.missing == ("reporting.tasks_per_month", "troubleshooting.current_minutes")


def test_with_nothing_configured_every_figure_is_unknown_and_every_input_is_named_in_a_fixed_order():
    report = calculate_sortview_value(SortViewValueSettings(), None)

    assert report.reporting == REPORTING_NONE and report.troubleshooting == REPORTING_NONE
    assert [getattr(report, field.name) for field in dataclasses.fields(report)][2:8] == [None] * 6
    assert report.missing == USED_INPUTS == (
        "labor_rate",
        "subscription_annual_cost",
        "reporting.tasks_per_month",
        "reporting.baseline_minutes_per_task",
        "reporting.current_minutes_per_task",
        "troubleshooting.incidents_per_month",
        "troubleshooting.staff_per_incident",
        "troubleshooting.baseline_minutes",
        "troubleshooting.current_minutes",
    )


def test_missing_keeps_its_order_whatever_was_left_out_and_never_names_a_basis_or_a_date():
    block = _block(**{"troubleshooting.current_minutes": ..., "labor_rate": ..., "reporting.tasks_per_month": ..., "reporting.basis": ..., "troubleshooting.as_of": ...})

    assert _report(block).missing == ("labor_rate", "reporting.tasks_per_month", "troubleshooting.current_minutes")
    assert not any("basis" in name or "as_of" in name for name in USED_INPUTS)


def test_a_rate_borrowed_from_efficiency_is_not_missing():
    report = _report(_block(labor_rate=...), OrganizationEfficiencySettings(labor_rate=D("32.50")))

    assert report.missing == ()
    assert report.monthly_value == D("595.83")


# --- nothing is clipped ----------------------------------------------------------------------------------------------

def test_work_that_takes_longer_now_gives_negative_hours_and_a_negative_value_as_they_are():
    # Reporting now takes 120 minutes a task where it took 90: 12 * 30 / 60 = 6 hours more a month.
    report = _report(_block(**{"reporting.current_minutes_per_task": "120"}))

    assert report.reporting == CategoryValue(D("18.00"), D("24.00"), D("-6.00"), D("-195.00"), D("-2340.00"))
    # Set against troubleshooting's 3.333... hours recovered: 2.666... hours more a month overall.
    assert report.recovered_hours_per_month == D("-2.67")
    assert report.monthly_value == D("-86.67")
    assert report.annual_value == D("-1040.00")
    assert report.net_annual_value == D("-5840.00")
    assert report.roi_percent == D("-121.7")                    # -5,840 / 4,800
    # A value that never covers the cost has no payback: not zero months, and not a negative number of them.
    assert report.payback_months is None
    assert report.missing == ()


def test_a_positive_value_smaller_than_the_cost_is_a_negative_return_and_a_payback_longer_than_a_year():
    report = _report(_block(subscription_annual_cost="10000"))

    assert report.net_annual_value == D("-2850.00")
    assert report.roi_percent == D("-28.5")
    assert report.payback_months == D("16.8")                   # 10,000 / 595.8333...


def test_a_monthly_value_of_exactly_zero_has_no_payback_and_a_return_of_minus_everything():
    # Reporting recovers 15 hours; troubleshooting takes 15 more: 8 * 112.5 / 60 = 15.
    block = _block(**{"troubleshooting.baseline_minutes": "0", "troubleshooting.current_minutes": "112.5"})
    report = _report(block)

    assert report.troubleshooting.recovered_hours_per_month == D("-15.00")
    assert report.recovered_hours_per_month == D("0.00")
    assert report.monthly_value == D("0.00")
    assert report.annual_value == D("0.00")
    assert report.net_annual_value == D("-4800.00")
    assert report.roi_percent == D("-100.0")
    assert report.payback_months is None


def test_no_change_at_all_is_zero_and_is_never_written_as_minus_zero():
    block = _block(**{"reporting.current_minutes_per_task": "90", "troubleshooting.current_minutes": "45", "subscription_annual_cost": "0"})
    report = _report(block)

    figures = [report.recovered_hours_per_month, report.monthly_value, report.annual_value, report.net_annual_value,
               report.reporting.recovered_hours_per_month, report.troubleshooting.monthly_value]
    assert [str(figure) for figure in figures] == ["0.00"] * 6
    assert not any(figure.is_signed() for figure in figures)
    assert report.roi_percent is None and report.payback_months is None


def test_a_loss_too_small_to_report_is_zero_not_minus_zero():
    # 0.1 tasks, 0.1 minutes longer each: 0.0001666... hours more, which is 0.00.
    block = _block(**{"reporting.tasks_per_month": "0.1", "reporting.baseline_minutes_per_task": "1.0", "reporting.current_minutes_per_task": "1.1"})

    assert str(_report(block).reporting.recovered_hours_per_month) == "0.00"
    assert str(_report(block).reporting.monthly_value) == "-0.01"       # 0.0001666... * 32.50 = 0.0054..., half up


def test_nothing_in_the_arithmetic_takes_a_floor_or_an_absolute_value_of_a_result():
    code = inspect.getsource(sortview_value).split('"""', 2)[2]

    for clipped in ("max(", "min(", " abs(", ".clip", "clamp", "if recovered <", "if monthly_value <", "< 0"):
        assert clipped not in code, clipped
    # The one place a sign is dropped is a zero's.
    assert code.count("copy_abs") == 1 and "rounded.is_zero()" in code


# --- what describes the figures changes none of them ---------------------------------------------------------------

@pytest.mark.parametrize(
    "changes",
    [
        {"reporting.basis": "measured", "troubleshooting.basis": "estimate"},
        {"reporting.basis": ..., "troubleshooting.basis": ...},
        {"reporting.as_of": "2019-01-01", "troubleshooting.as_of": "2026-10-06"},
        {"reporting.as_of": ..., "troubleshooting.as_of": ..., "reporting.basis": ..., "troubleshooting.basis": ...},
    ],
)
def test_a_basis_and_an_as_of_date_change_no_figure_and_are_never_missing(changes):
    assert _report(_block(**changes)) == _report(BLOCK)
    assert _report(_block(**changes)).missing == ()


def test_the_arithmetic_never_reads_a_basis_or_a_date():
    source = inspect.getsource(calculate_sortview_value) + inspect.getsource(sortview_value._category_value)

    assert "basis" not in source and "as_of" not in source and "date" not in source


# --- exact, then rounded once ----------------------------------------------------------------------------------------

def test_every_figure_is_rounded_half_up_to_its_own_places():
    # 1 task, 0.3 minutes saved a task: 0.005 hours exactly -- half a cent of an hour, rounded up.
    half = _report(_block(**{"reporting.tasks_per_month": "1", "reporting.baseline_minutes_per_task": "0.3", "reporting.current_minutes_per_task": "0"}))
    assert half.reporting.recovered_hours_per_month == D("0.01")
    # 0.005 * 32.50 = 0.1625 -> 0.16; * 12 = 1.95.
    assert half.reporting.monthly_value == D("0.16")
    assert half.reporting.annual_value == D("1.95")

    report = _report()
    for figure, places in [
        (report.recovered_hours_per_month, -2), (report.reporting.baseline_hours_per_month, -2), (report.troubleshooting.current_hours_per_month, -2),
        (report.monthly_value, -2), (report.annual_value, -2), (report.net_annual_value, -2), (report.troubleshooting.annual_value, -2),
        (report.roi_percent, -1), (report.payback_months, -1),
    ]:
        assert figure.as_tuple().exponent == places, figure
    assert (sortview_value.HOURS_PLACES, sortview_value.MONEY_PLACES) == (D("0.01"), D("0.01"))
    assert (sortview_value.ROI_PLACES, sortview_value.PAYBACK_PLACES) == (D("0.1"), D("0.1"))


@pytest.mark.parametrize(
    "cost, roi",
    [
        # Net 2,350 over the cost, in percent: each rounded once, half up, to one place.
        ("4700.00", "52.1"),       # 2,450 / 4,700 = 52.1276...
        ("4000.00", "78.8"),       # 3,150 / 4,000 = 78.75 exactly -> half up
        ("8000.00", "-10.6"),      # -850 / 8,000 = -10.625 -> half up, away from zero
        ("7150.00", "0.0"),        # nothing over the cost
        ("7151.00", "0.0"),        # -1 / 7,151 = -0.0139...%: zero, and not minus zero
    ],
)
def test_the_return_is_rounded_once_half_up_to_one_place(cost, roi):
    report = _report(_block(subscription_annual_cost=cost))

    assert str(report.roi_percent) == roi


def test_no_figure_is_made_from_another_figures_rounded_value():
    report = _report()

    # Rounded monthly values would give 5,850.00 + 1,299.96 = 7,149.96 a year, and 595.83 * 12 the same. The exact
    # ones give 7,150.00: troubleshooting's 108.333... a month is 1,300.00 a year.
    assert report.troubleshooting.monthly_value * 12 == D("1299.96")
    assert report.troubleshooting.annual_value == D("1300.00")
    assert report.monthly_value * 12 == D("7149.96")
    assert report.annual_value == D("7150.00")
    # Hours likewise: 15.00 + 3.33 would be 18.33 either way here, but 18.00 - 2.67 is 15.33, not troubleshooting's 3.33.
    assert report.troubleshooting.baseline_hours_per_month - report.troubleshooting.current_hours_per_month == D("3.33")
    # And the return is of the exact net over the cost, not of the rounded figures' quotient rounded twice.
    assert report.roi_percent == (D("2350") / D("4800") * 100).quantize(D("0.1"), rounding=decimal.ROUND_HALF_UP)


def test_thirds_do_not_drift_however_many_are_added_up():
    # Each kind of work recovers a third of an hour a month: 1 task of 20 minutes, and 1 incident of 1 person for 20.
    block = {
        "labor_rate": "30.00",
        "subscription_annual_cost": "100.00",
        "reporting": {"tasks_per_month": "1", "baseline_minutes_per_task": "20", "current_minutes_per_task": "0"},
        "troubleshooting": {"incidents_per_month": "1", "staff_per_incident": "1", "baseline_minutes": "20", "current_minutes": "0"},
    }
    report = _report(block)

    assert report.reporting.recovered_hours_per_month == D("0.33")
    assert report.recovered_hours_per_month == D("0.67")        # two thirds -- not 0.33 + 0.33
    assert report.reporting.monthly_value == D("10.00")
    assert report.monthly_value == D("20.00")
    assert report.annual_value == D("240.00")
    assert report.roi_percent == D("140.0")
    assert report.payback_months == D("5.0")


def test_the_result_does_not_depend_on_the_process_decimal_context():
    settings = _parse(BLOCK)
    rate = resolve_labor_rate(settings, NO_EFFICIENCY)
    expected = calculate_sortview_value(settings, rate)

    with decimal.localcontext() as context:
        context.prec = 3
        context.rounding = decimal.ROUND_DOWN
        assert calculate_sortview_value(settings, rate) == expected
    with decimal.localcontext(decimal.Context(prec=6, rounding=decimal.ROUND_HALF_EVEN, traps=[decimal.Inexact])):
        with pytest.raises(decimal.Inexact):
            _ = Decimal(1) / Decimal(3)
        assert calculate_sortview_value(settings, rate) == expected


def test_no_figure_has_been_through_a_float():
    code = inspect.getsource(sortview_value).split('"""', 2)[2]

    assert "float" not in code and "round(" not in code
    report = _report()
    assert all(isinstance(value, Decimal) for value in dataclasses.astuple(report.reporting) + dataclasses.astuple(report.troubleshooting))
    assert all(isinstance(getattr(report, name), Decimal) for name in ("recovered_hours_per_month", "monthly_value", "annual_value", "net_annual_value", "roi_percent", "payback_months"))


def test_the_largest_values_a_block_can_hold_are_worked_out_in_full():
    block = {
        "labor_rate": "10000.00",
        "subscription_annual_cost": "10000000.00",
        "reporting": {"tasks_per_month": "100000", "baseline_minutes_per_task": "10000", "current_minutes_per_task": "0"},
        "troubleshooting": {"incidents_per_month": "100000", "staff_per_incident": "1000", "baseline_minutes": "10000", "current_minutes": "0.1"},
    }
    report = _report(block)

    # 100,000 * 10,000 / 60 hours a month.
    assert report.reporting.baseline_hours_per_month == D("16666666.67")
    assert report.troubleshooting.baseline_hours_per_month == D("16666666666.67")
    assert report.troubleshooting.current_hours_per_month == D("166666.67")
    # 16,666,666.666... + 16,666,500,000 hours a month, at 10,000 an hour, for twelve months.
    assert report.annual_value == D("2001980000000000.00")
    assert "E" not in "".join(str(value) for value in dataclasses.astuple(report) if isinstance(value, Decimal))
    assert report.payback_months == D("0.0")


def test_the_smallest_values_are_not_lost():
    block = {
        "labor_rate": "0.01",
        "subscription_annual_cost": "0.01",
        "reporting": {"tasks_per_month": "0.1", "baseline_minutes_per_task": "0.1", "current_minutes_per_task": "0"},
        "troubleshooting": {"incidents_per_month": "0.1", "staff_per_incident": "0.1", "baseline_minutes": "0.1", "current_minutes": "0"},
    }
    report = _report(block)

    # Too small to report as hours or money -- and still positive where it decides something.
    assert report.recovered_hours_per_month == D("0.00")
    assert report.monthly_value == D("0.00")
    # (0.01 + 0.001) / 60 hours * 0.01 a month is more than nothing: there is a payback, a very long one.
    assert report.payback_months is not None and report.payback_months > 1000
    assert report.roi_percent == D("-99.8")


# =====================================================================================================================
# The shape of it
# =====================================================================================================================

def test_the_report_is_exactly_these_figures_and_cannot_be_changed():
    assert [field.name for field in dataclasses.fields(SortViewValueReport)] == [
        "reporting",
        "troubleshooting",
        "recovered_hours_per_month",
        "monthly_value",
        "annual_value",
        "net_annual_value",
        "roi_percent",
        "payback_months",
        "missing",
    ]
    assert [field.name for field in dataclasses.fields(CategoryValue)] == [
        "baseline_hours_per_month",
        "current_hours_per_month",
        "recovered_hours_per_month",
        "monthly_value",
        "annual_value",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _report().monthly_value = D("1")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _parse(BLOCK).labor_rate = D("1")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _parse(BLOCK).reporting.tasks_per_month = D("1")  # type: ignore[misc]


def test_the_settings_are_exactly_these_fields():
    assert [field.name for field in dataclasses.fields(SortViewValueSettings)] == ["labor_rate", "subscription_annual_cost", "reporting", "troubleshooting"]
    assert [field.name for field in dataclasses.fields(ReportingInputs)] == [
        "tasks_per_month", "baseline_minutes_per_task", "current_minutes_per_task", "basis", "as_of",
    ]
    assert [field.name for field in dataclasses.fields(TroubleshootingInputs)] == [
        "incidents_per_month", "staff_per_incident", "baseline_minutes", "current_minutes", "basis", "as_of",
    ]
    assert SORTVIEW_VALUE_SETTINGS_KEY == "sortview_value"
    assert (REPORTING, TROUBLESHOOTING) == ("reporting", "troubleshooting")


def test_there_are_two_kinds_of_work_and_no_third():
    assert list(sortview_value._CATEGORY_FIELDS) == ["reporting", "troubleshooting"]
    code = inspect.getsource(sortview_value).split('"""', 2)[2].lower()
    for later in ("ad_hoc", "adhoc", "vendor", "category_c", "other_work", "baseline_period", "time_study", "days_remaining"):
        assert later not in code, later


def test_nothing_here_claims_time_or_money_was_saved_or_takes_a_figure_from_a_sorter():
    source = inspect.getsource(sortview_value).lower()

    for claim in ("hours saved", "hours_saved", "savings", "time saved", "staff time avoided", "guarantee"):
        assert claim not in source, claim
    for borrowed in ("checkin", "check_in", "manual_items_per_hour", "amh_rate", "items_per_hour"):
        assert borrowed not in source.split('"""', 2)[2], borrowed


def test_the_module_is_pure():
    source = inspect.getsource(sortview_value)

    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == [
        "from __future__ import annotations",
        "from collections.abc import Mapping",
        "from dataclasses import dataclass",
        "from datetime import date",
        "from decimal import ROUND_HALF_UP, Context, Decimal",
        "from typing import Any, Literal",
        "from services.efficiency_settings import (",
    ]
    code = source.split('"""', 2)[2]
    for forbidden in ("date.today", ".now(", "getenv", "sqlalchemy", "fastapi", "streamlit", "get_engine", "execute(", "open(", "requests"):
        assert forbidden not in code, forbidden


def test_it_uses_efficiencys_own_checks_as_they_are_and_changes_nothing_of_efficiency():
    from services import efficiency_settings

    assert sortview_value._decimal is efficiency_settings._decimal
    assert sortview_value._date is efficiency_settings._date
    assert sortview_value._DecimalField is efficiency_settings._DecimalField
    assert sortview_value.NOT_AN_OBJECT == "not_an_object" and sortview_value.UNKNOWN_FIELD == "unknown_field"
    # Efficiency's own limits are its own: a labor rate there still stops at 1,000.
    assert efficiency_settings._FIELDS["labor_rate"].maximum == 1000
    assert sortview_value._FIELDS["labor_rate"].maximum == 10_000
    assert "sortview_value" not in inspect.getsource(efficiency_settings)
