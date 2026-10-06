"""R6A: Efficiency assumptions -- the pure settings model.

    parse_*_efficiency_settings(document)        what a stored settings document says
    validate_*_efficiency_settings(block)        what someone is about to store
    serialize_*_efficiency_settings(settings)    the block to store, as canonical text
    resolve_efficiency_settings(org, sorter)     what applies to one sorter

Nothing here touches a database or a clock.

Imported the "flat" way (services.efficiency_settings), the identity the API
process uses.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
from datetime import date
from decimal import Decimal

import pytest

from services import efficiency_settings
from services.efficiency_settings import (
    EFFICIENCY_SETTINGS_KEY,
    ORGANIZATION_FIELDS,
    SORTER_FIELDS,
    EffectiveEfficiencySettings,
    EfficiencySettingProblem,
    EfficiencySettingsError,
    OrganizationEfficiencySettings,
    ResolvedRate,
    SorterEfficiencySettings,
    parse_organization_efficiency_settings,
    parse_sorter_efficiency_settings,
    resolve_efficiency_settings,
    serialize_organization_efficiency_settings,
    serialize_sorter_efficiency_settings,
    validate_organization_efficiency_settings,
    validate_sorter_efficiency_settings,
)
from services.tenant_service import _deep_merge_settings

# The product's date a write is validated against. Always passed in as today=: nothing under test reads a clock.
AS_OF = date(2026, 10, 6)

ORG_DOCUMENT = {
    "library_name": "Example Library",
    "security": {"admin_enabled": True, "admin_password_hash": "not-a-real-hash"},
    "transit": {"home_branch_label": "Main", "destinations": [{"key": "b1", "label": "Westside", "enabled": True}]},
    "efficiency": {"labor_rate": "20.00", "manual_items_per_hour": "45.0"},
}
SORTER_DOCUMENT = {
    "branch_name": "Central",
    "efficiency": {
        "labor_rate": "25.00",
        "manual_items_per_hour": "50.0",
        "one_time_cost": "118003.92",
        "recurring_annual_cost": "8400.00",
        "in_service_date": "2020-11-20",
    },
}


def _org(**efficiency) -> dict:
    return {"efficiency": efficiency}


def _problems(call) -> list[tuple[str, str]]:
    with pytest.raises(EfficiencySettingsError) as raised:
        call()
    return [(problem.field, problem.code) for problem in raised.value.problems]


# =====================================================================================================================
# Organization documents
# =====================================================================================================================

def test_an_organization_document_gives_both_defaults_as_decimals():
    settings = parse_organization_efficiency_settings(ORG_DOCUMENT)

    assert settings == OrganizationEfficiencySettings(labor_rate=Decimal("20.00"), manual_items_per_hour=Decimal("45.0"))
    assert isinstance(settings.labor_rate, Decimal)
    assert str(settings.labor_rate) == "20.00"
    assert str(settings.manual_items_per_hour) == "45.0"


@pytest.mark.parametrize(
    ("efficiency", "expected"),
    [
        ({"labor_rate": "20.00"}, OrganizationEfficiencySettings(labor_rate=Decimal("20.00"))),
        ({"manual_items_per_hour": "45.0"}, OrganizationEfficiencySettings(manual_items_per_hour=Decimal("45.0"))),
        ({"labor_rate": None, "manual_items_per_hour": "45.0"}, OrganizationEfficiencySettings(manual_items_per_hour=Decimal("45.0"))),
        ({}, OrganizationEfficiencySettings()),
    ],
)
def test_a_missing_or_null_organization_field_is_unknown_and_has_no_default(efficiency, expected):
    assert parse_organization_efficiency_settings(_org(**efficiency)) == expected


@pytest.mark.parametrize(
    "document",
    [{}, {"transit": {"home_branch_label": "Main"}}, {"efficiency": None}, None, "", "{}", [], 0],
)
def test_a_document_with_no_efficiency_block_is_valid_and_says_nothing(document):
    assert parse_organization_efficiency_settings(document) == OrganizationEfficiencySettings(None, None)
    assert parse_sorter_efficiency_settings(document) == SorterEfficiencySettings(None, None, None, None, None)


def test_there_is_no_built_in_rate_anywhere_in_the_module():
    source = inspect.getsource(efficiency_settings)

    for stale_default in ("17.56", "18.0", "118003", "8400", "2020-11-20", "130"):
        assert stale_default not in source
    # The old manual rate: no "45" as a number of items per hour.
    assert "45" not in source.replace("0-9", "")


@pytest.mark.parametrize("block", ["20.00", ["labor_rate", "20.00"], 20, True, 0, "", []])
def test_an_efficiency_block_that_is_not_an_object_is_refused(block):
    document = {"efficiency": block}

    assert _problems(lambda: parse_organization_efficiency_settings(document)) == [("efficiency", "not_an_object")]
    assert _problems(lambda: parse_sorter_efficiency_settings(document)) == [("efficiency", "not_an_object")]
    assert _problems(lambda: validate_organization_efficiency_settings(block)) == [("efficiency", "not_an_object")]
    assert _problems(lambda: validate_sorter_efficiency_settings(block, today=AS_OF)) == [("efficiency", "not_an_object")]


def test_every_other_key_of_a_document_is_ignored_however_malformed():
    document = {**ORG_DOCUMENT, "transit": "not an object", "security": None, "labor_rate": "999.99", "efficiency_v2": {"labor_rate": "1"}}

    assert parse_organization_efficiency_settings(document) == parse_organization_efficiency_settings(ORG_DOCUMENT)


def test_reading_an_organization_document_ignores_fields_that_are_not_the_organizations():
    # A sorter's cost written at organization level by mistake, malformed or not, and a key from some later version.
    document = _org(labor_rate="20.00", one_time_cost="100000.00", recurring_annual_cost="lots", in_service_date="soon", updated_by=7)

    assert parse_organization_efficiency_settings(document) == OrganizationEfficiencySettings(labor_rate=Decimal("20.00"))
    assert [field.name for field in dataclasses.fields(OrganizationEfficiencySettings)] == ["labor_rate", "manual_items_per_hour"]


def test_a_malformed_organization_field_is_an_error_that_names_every_field_and_quotes_no_value():
    document = _org(labor_rate="$17.56", manual_items_per_hour=45)

    with pytest.raises(EfficiencySettingsError) as raised:
        parse_organization_efficiency_settings(document)

    assert raised.value.problems == (
        EfficiencySettingProblem("labor_rate", "not_a_decimal"),
        EfficiencySettingProblem("manual_items_per_hour", "not_a_string"),
    )
    assert str(raised.value) == "Invalid efficiency settings: labor_rate (not_a_decimal), manual_items_per_hour (not_a_string)"
    assert "17.56" not in str(raised.value)
    assert isinstance(raised.value, ValueError)


# =====================================================================================================================
# Sorter documents
# =====================================================================================================================

def test_a_sorter_document_gives_its_overrides_and_its_own_costs_and_date():
    assert parse_sorter_efficiency_settings(SORTER_DOCUMENT) == SorterEfficiencySettings(
        labor_rate=Decimal("25.00"),
        manual_items_per_hour=Decimal("50.0"),
        one_time_cost=Decimal("118003.92"),
        recurring_annual_cost=Decimal("8400.00"),
        in_service_date=date(2020, 11, 20),
    )


def test_a_missing_sorter_field_is_unknown_not_zero():
    settings = parse_sorter_efficiency_settings(_org(recurring_annual_cost="8400.00"))

    assert settings == SorterEfficiencySettings(recurring_annual_cost=Decimal("8400.00"))
    assert settings.one_time_cost is None
    assert settings.in_service_date is None
    assert settings.labor_rate is None


def test_an_explicit_zero_cost_is_zero_and_is_not_the_same_as_unknown():
    zero = parse_sorter_efficiency_settings(_org(one_time_cost="0.00", recurring_annual_cost="0"))
    unknown = parse_sorter_efficiency_settings(_org())

    assert zero.one_time_cost == Decimal("0.00")
    assert zero.recurring_annual_cost == Decimal("0.00")
    assert zero.one_time_cost is not None
    assert zero != unknown
    assert serialize_sorter_efficiency_settings(zero) == {"one_time_cost": "0.00", "recurring_annual_cost": "0.00"}
    assert serialize_sorter_efficiency_settings(unknown) == {}


def test_a_malformed_sorter_block_reports_every_field_at_fault_in_field_order():
    document = _org(
        in_service_date="2026-02-30", one_time_cost="-1.00", labor_rate="0.00", recurring_annual_cost="1e4", manual_items_per_hour="45.25"
    )

    assert _problems(lambda: parse_sorter_efficiency_settings(document)) == [
        ("labor_rate", "out_of_range"),
        ("manual_items_per_hour", "too_many_decimal_places"),
        ("one_time_cost", "not_a_decimal"),
        ("recurring_annual_cost", "not_a_decimal"),
        ("in_service_date", "not_a_date"),
    ]


def test_reading_a_sorter_document_ignores_keys_it_does_not_know():
    document = _org(one_time_cost="5.00", amh_rate="130", updated_at="2026-10-06T12:00:00Z")

    assert parse_sorter_efficiency_settings(document) == SorterEfficiencySettings(one_time_cost=Decimal("5.00"))


# =====================================================================================================================
# Decimal text
# =====================================================================================================================

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("17.56", "17.56"),
        ("25.00", "25.00"),
        ("17.5", "17.50"),
        ("17", "17.00"),
        ("0.01", "0.01"),
        ("1000", "1000.00"),
        ("1000.00", "1000.00"),
    ],
)
def test_a_labor_rate_is_plain_decimal_text_kept_to_two_places(text, expected):
    settings = parse_organization_efficiency_settings(_org(labor_rate=text))

    assert settings.labor_rate == Decimal(expected)
    assert str(settings.labor_rate) == expected
    assert serialize_organization_efficiency_settings(settings) == {"labor_rate": expected}


@pytest.mark.parametrize(
    ("value", "code"),
    [
        # More places than the field keeps: refused, never rounded -- even when the extra digit is a zero.
        ("17.560", "too_many_decimal_places"),
        ("17.567", "too_many_decimal_places"),
        ("17.5600000000000000000000000001", "too_many_decimal_places"),
        # Not plain decimal text.
        ("1e2", "not_a_decimal"),
        ("1E2", "not_a_decimal"),
        ("NaN", "not_a_decimal"),
        ("nan", "not_a_decimal"),
        ("Infinity", "not_a_decimal"),
        ("-Infinity", "not_a_decimal"),
        ("-0.00", "not_a_decimal"),
        ("-17.56", "not_a_decimal"),
        ("+17.56", "not_a_decimal"),
        ("00017.56", "not_a_decimal"),
        ("017.56", "not_a_decimal"),
        (" 17.56 ", "not_a_decimal"),
        ("17.56\n", "not_a_decimal"),
        ("$17.56", "not_a_decimal"),
        ("17.56 USD", "not_a_decimal"),
        ("1,000.00", "not_a_decimal"),
        ("17,56", "not_a_decimal"),
        ("17.", "not_a_decimal"),
        (".56", "not_a_decimal"),
        ("1_000", "not_a_decimal"),
        ("0x10", "not_a_decimal"),
        ("١٧.٥٦", "not_a_decimal"),
        ("", "not_a_decimal"),
        ("seventeen", "not_a_decimal"),
        ("1" * 33, "not_a_decimal"),
        # Not text at all. A JSON number has already been through a binary float.
        (17.56, "not_a_string"),
        (17, "not_a_string"),
        (0, "not_a_string"),
        (True, "not_a_string"),
        (Decimal("17.56"), "not_a_string"),
        (["17.56"], "not_a_string"),
        ({"amount": "17.56"}, "not_a_string"),
        # Out of range: an hour of work is worth something, and no more than 1,000.
        ("0", "out_of_range"),
        ("0.00", "out_of_range"),
        ("1000.01", "out_of_range"),
        ("1001", "out_of_range"),
        ("99999999999999999999999999999999", "out_of_range"),
    ],
)
def test_a_labor_rate_that_is_not_one_is_refused_with_a_reason(value, code):
    assert _problems(lambda: parse_organization_efficiency_settings(_org(labor_rate=value))) == [("labor_rate", code)]
    assert _problems(lambda: validate_organization_efficiency_settings({"labor_rate": value})) == [("labor_rate", code)]


def test_a_json_number_is_refused_exactly_as_json_delivers_it():
    document = json.loads('{"efficiency": {"labor_rate": 17.56, "manual_items_per_hour": 45}}')

    assert _problems(lambda: parse_organization_efficiency_settings(document)) == [
        ("labor_rate", "not_a_string"),
        ("manual_items_per_hour", "not_a_string"),
    ]


@pytest.mark.parametrize(
    ("field", "accepted", "refused"),
    [
        ("manual_items_per_hour", {"1": "1.0", "1.0": "1.0", "45": "45.0", "47.1": "47.1", "1000.0": "1000.0"}, ["0", "0.9", "1000.1", "1001"]),
        ("one_time_cost", {"0": "0.00", "0.00": "0.00", "118003.92": "118003.92", "100000000": "100000000.00"}, ["100000000.01", "100000001"]),
        ("recurring_annual_cost", {"0": "0.00", "0.0": "0.00", "8400": "8400.00", "10000000.00": "10000000.00"}, ["10000000.01", "10000001"]),
    ],
)
def test_each_field_has_its_own_bounds_both_ends_included(field, accepted, refused):
    for text, canonical in accepted.items():
        settings = parse_sorter_efficiency_settings(_org(**{field: text}))
        assert str(getattr(settings, field)) == canonical
        assert serialize_sorter_efficiency_settings(settings) == {field: canonical}
    for text in refused:
        assert _problems(lambda text=text: parse_sorter_efficiency_settings(_org(**{field: text}))) == [(field, "out_of_range")]


@pytest.mark.parametrize(
    ("field", "text"),
    [("manual_items_per_hour", "45.00"), ("manual_items_per_hour", "45.25"), ("one_time_cost", "1.005"), ("recurring_annual_cost", "8400.000")],
)
def test_no_field_takes_more_decimal_places_than_it_keeps(field, text):
    assert _problems(lambda: parse_sorter_efficiency_settings(_org(**{field: text}))) == [(field, "too_many_decimal_places")]


def test_nothing_is_computed_in_binary_floating_point():
    source = inspect.getsource(efficiency_settings)

    assert "float(" not in source
    assert "round(" not in source
    # 0.1 + 0.2 is exactly 0.3 here.
    settings = parse_sorter_efficiency_settings(_org(one_time_cost="0.10", recurring_annual_cost="0.20"))
    assert settings.one_time_cost + settings.recurring_annual_cost == Decimal("0.30")


# =====================================================================================================================
# The in-service date
# =====================================================================================================================

@pytest.mark.parametrize("text", ["2020-11-20", "2024-02-29", "2000-02-29", "1999-12-31", "2026-10-06", "2026-10-05"])
def test_an_in_service_date_is_a_real_calendar_date_up_to_and_including_today(text):
    stored = parse_sorter_efficiency_settings(_org(in_service_date=text))
    written = validate_sorter_efficiency_settings({"in_service_date": text}, today=AS_OF)

    assert stored.in_service_date == written.in_service_date == date.fromisoformat(text)
    assert serialize_sorter_efficiency_settings(written) == {"in_service_date": text}


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("2026-02-30", "not_a_date"),
        ("2025-02-29", "not_a_date"),
        ("2026-13-01", "not_a_date"),
        ("2026-00-10", "not_a_date"),
        ("2020-11-20T00:00:00", "not_a_date"),
        ("2020-11-20 00:00", "not_a_date"),
        ("2020-11-20T00:00:00Z", "not_a_date"),
        ("2020-11-20Z", "not_a_date"),
        ("2020-11-20+00:00", "not_a_date"),
        ("20201120", "not_a_date"),
        ("2020-W47-5", "not_a_date"),
        ("2020-1-2", "not_a_date"),
        ("11/20/2020", "not_a_date"),
        (" 2020-11-20", "not_a_date"),
        ("٢٠٢٠-١١-٢٠", "not_a_date"),
        ("", "not_a_date"),
        ("today", "not_a_date"),
        (20201120, "not_a_string"),
        (1605830400.0, "not_a_string"),
        (True, "not_a_string"),
        (["2020-11-20"], "not_a_string"),
    ],
)
def test_an_in_service_date_that_is_not_a_plain_date_is_refused(value, code):
    assert _problems(lambda: parse_sorter_efficiency_settings(_org(in_service_date=value))) == [("in_service_date", code)]
    assert _problems(lambda: validate_sorter_efficiency_settings({"in_service_date": value}, today=AS_OF)) == [("in_service_date", code)]


def test_a_future_in_service_date_is_refused_when_written_against_the_date_supplied():
    tomorrow = {"in_service_date": "2026-10-07"}

    assert _problems(lambda: validate_sorter_efficiency_settings(tomorrow, today=AS_OF)) == [("in_service_date", "in_the_future")]
    # The same block is fine a day later: "today" is whatever the caller says it is.
    assert validate_sorter_efficiency_settings(tomorrow, today=date(2026, 10, 7)).in_service_date == date(2026, 10, 7)


def test_today_must_be_supplied_and_no_clock_is_read():
    with pytest.raises(TypeError):
        validate_sorter_efficiency_settings({})  # type: ignore[call-arg]

    source = inspect.getsource(efficiency_settings)
    for clock in ("date.today", "datetime.now", "utcnow", "time.time", "import time", "import datetime\n"):
        assert clock not in source


def test_reading_a_stored_date_does_not_depend_on_what_day_it_is():
    # Stored by a server whose "today" was a day ahead of this reader's. Reading does not second-guess it.
    assert parse_sorter_efficiency_settings(_org(in_service_date="2999-01-01")).in_service_date == date(2999, 1, 1)
    assert "today" not in inspect.signature(parse_sorter_efficiency_settings).parameters


# =====================================================================================================================
# Writing: stricter than reading
# =====================================================================================================================

def test_a_write_takes_the_block_itself_and_gives_the_same_settings_as_reading_it_back():
    organization = validate_organization_efficiency_settings(ORG_DOCUMENT["efficiency"])
    sorter = validate_sorter_efficiency_settings(SORTER_DOCUMENT["efficiency"], today=AS_OF)

    assert organization == parse_organization_efficiency_settings(ORG_DOCUMENT)
    assert sorter == parse_sorter_efficiency_settings(SORTER_DOCUMENT)


@pytest.mark.parametrize("block", [None, {}, {"labor_rate": None, "manual_items_per_hour": None}])
def test_a_write_of_nothing_sets_nothing(block):
    assert validate_organization_efficiency_settings(block) == OrganizationEfficiencySettings()
    assert validate_sorter_efficiency_settings(block, today=AS_OF) == SorterEfficiencySettings()
    assert serialize_organization_efficiency_settings(validate_organization_efficiency_settings(block)) == {}


@pytest.mark.parametrize("field", ["one_time_cost", "recurring_annual_cost", "in_service_date"])
def test_a_sorters_field_cannot_be_written_to_an_organization(field):
    block = {"labor_rate": "20.00", field: SORTER_DOCUMENT["efficiency"][field]}

    assert _problems(lambda: validate_organization_efficiency_settings(block)) == [(field, "unknown_field")]


def test_a_write_refuses_every_key_it_does_not_know_alongside_any_malformed_field():
    block = {"labor_rate": "abc", "amh_rate": "130", "currency": "USD", "updated_by": 7}

    assert _problems(lambda: validate_sorter_efficiency_settings(block, today=AS_OF)) == [
        ("labor_rate", "not_a_decimal"),
        ("amh_rate", "unknown_field"),
        ("currency", "unknown_field"),
        ("updated_by", "unknown_field"),
    ]
    assert _problems(lambda: validate_organization_efficiency_settings({"efficiency": {"labor_rate": "20.00"}})) == [
        ("efficiency", "unknown_field")
    ]


# =====================================================================================================================
# Serializing
# =====================================================================================================================

def test_serializing_gives_canonical_text_for_the_block_alone_in_field_order():
    sorter = validate_sorter_efficiency_settings(
        {"in_service_date": "2020-11-20", "recurring_annual_cost": "8400", "one_time_cost": "118003.92","manual_items_per_hour": "50", "labor_rate": "25"},
        today=AS_OF,
    )

    block = serialize_sorter_efficiency_settings(sorter)

    assert block == SORTER_DOCUMENT["efficiency"]
    assert list(block) == list(SORTER_FIELDS)
    assert all(type(value) is str for value in block.values())
    assert list(serialize_organization_efficiency_settings(parse_organization_efficiency_settings(ORG_DOCUMENT))) == list(ORGANIZATION_FIELDS)


def test_a_serialized_block_is_plain_json_text_and_reads_back_as_itself():
    for document, parse, serialize in (
        (ORG_DOCUMENT, parse_organization_efficiency_settings, serialize_organization_efficiency_settings),
        (SORTER_DOCUMENT, parse_sorter_efficiency_settings, serialize_sorter_efficiency_settings),
    ):
        block = serialize(parse(document))
        stored = json.loads(json.dumps({EFFICIENCY_SETTINGS_KEY: block}))

        assert stored == {"efficiency": document["efficiency"]}
        assert parse(stored) == parse(document)
        assert serialize(parse(stored)) == block


def test_a_serialized_block_holds_efficiency_fields_only():
    block = serialize_sorter_efficiency_settings(parse_sorter_efficiency_settings(SORTER_DOCUMENT))
    organization_block = serialize_organization_efficiency_settings(parse_organization_efficiency_settings(ORG_DOCUMENT))

    assert set(block) <= set(SORTER_FIELDS)
    assert set(organization_block) == {"labor_rate", "manual_items_per_hour"}
    # Nothing else of the document, no identifier of any kind, and no record of who or when.
    for forbidden in ("efficiency", "security", "transit", "library_name", "branch_name", "id", "customer_id", "branch_id", "updated_by", "updated_at"):
        assert forbidden not in block
        assert forbidden not in organization_block


def test_a_large_or_small_value_is_never_written_in_exponent_form():
    settings = SorterEfficiencySettings(one_time_cost=Decimal("1E+8"), recurring_annual_cost=Decimal("0E-2"), manual_items_per_hour=Decimal("1E+3"))

    assert serialize_sorter_efficiency_settings(settings) == {
        "manual_items_per_hour": "1000.0",
        "one_time_cost": "100000000.00",
        "recurring_annual_cost": "0.00",
    }


def test_no_function_modifies_the_document_or_block_it_is_given():
    organization, sorter = copy.deepcopy(ORG_DOCUMENT), copy.deepcopy(SORTER_DOCUMENT)
    malformed = {"security": {"admin_enabled": True}, "efficiency": {"labor_rate": "abc", "extra": [1, 2]}}
    malformed_before = copy.deepcopy(malformed)

    parse_organization_efficiency_settings(organization)
    parse_sorter_efficiency_settings(sorter)
    validate_sorter_efficiency_settings(sorter["efficiency"], today=AS_OF)
    with pytest.raises(EfficiencySettingsError):
        parse_organization_efficiency_settings(malformed)
    with pytest.raises(EfficiencySettingsError):
        validate_sorter_efficiency_settings(malformed["efficiency"], today=AS_OF)

    assert organization == ORG_DOCUMENT
    assert sorter == SORTER_DOCUMENT
    assert malformed == malformed_before


# =====================================================================================================================
# What applies to one sorter
# =====================================================================================================================

ORGANIZATION = OrganizationEfficiencySettings(labor_rate=Decimal("20.00"), manual_items_per_hour=Decimal("45.0"))


def test_a_sorter_with_nothing_of_its_own_inherits_both_rates_and_nothing_else():
    assert resolve_efficiency_settings(ORGANIZATION, SorterEfficiencySettings()) == EffectiveEfficiencySettings(
        labor_rate=ResolvedRate(Decimal("20.00"), "organization"),
        manual_items_per_hour=ResolvedRate(Decimal("45.0"), "organization"),
        one_time_cost=None,
        recurring_annual_cost=None,
        in_service_date=None,
    )


def test_a_sorters_labor_rate_overrides_the_organizations_and_only_that():
    effective = resolve_efficiency_settings(ORGANIZATION, SorterEfficiencySettings(labor_rate=Decimal("25.00")))

    assert effective.labor_rate == ResolvedRate(Decimal("25.00"), "sorter")
    assert effective.manual_items_per_hour == ResolvedRate(Decimal("45.0"), "organization")


def test_a_sorters_manual_rate_overrides_the_organizations_and_only_that():
    effective = resolve_efficiency_settings(ORGANIZATION, SorterEfficiencySettings(manual_items_per_hour=Decimal("50.0")))

    assert effective.labor_rate == ResolvedRate(Decimal("20.00"), "organization")
    assert effective.manual_items_per_hour == ResolvedRate(Decimal("50.0"), "sorter")


def test_a_rate_nobody_set_is_unknown_and_has_no_source():
    effective = resolve_efficiency_settings(OrganizationEfficiencySettings(), SorterEfficiencySettings(labor_rate=Decimal("25.00")))

    assert effective.labor_rate == ResolvedRate(Decimal("25.00"), "sorter")
    assert effective.manual_items_per_hour is None
    assert resolve_efficiency_settings(OrganizationEfficiencySettings(), SorterEfficiencySettings()) == EffectiveEfficiencySettings(
        None, None, None, None, None
    )


def test_a_cost_written_in_the_organizations_document_is_never_a_sorters_cost():
    organization_document = _org(labor_rate="20.00", manual_items_per_hour="45.0", one_time_cost="100000.00", recurring_annual_cost="9000.00", in_service_date="2019-01-01")
    sorter_document = {"branch_name": "Central"}

    effective = resolve_efficiency_settings(
        parse_organization_efficiency_settings(organization_document), parse_sorter_efficiency_settings(sorter_document)
    )

    assert effective.one_time_cost is None
    assert effective.recurring_annual_cost is None
    assert effective.in_service_date is None
    assert effective.labor_rate == ResolvedRate(Decimal("20.00"), "organization")

    # The dashboard's effective settings WOULD hand all three down -- which is why Efficiency does not read them.
    merged = _deep_merge_settings(organization_document, sorter_document)
    assert parse_sorter_efficiency_settings(merged).one_time_cost == Decimal("100000.00")
    assert parse_sorter_efficiency_settings(merged).in_service_date == date(2019, 1, 1)


def test_a_sorters_explicit_zero_cost_is_zero_and_its_own_costs_and_date_are_kept():
    sorter = SorterEfficiencySettings(one_time_cost=Decimal("0.00"), recurring_annual_cost=Decimal("8400.00"), in_service_date=date(2020, 11, 20))

    effective = resolve_efficiency_settings(ORGANIZATION, sorter)

    assert effective.one_time_cost == Decimal("0.00")
    assert effective.one_time_cost is not None
    assert effective.recurring_annual_cost == Decimal("8400.00")
    assert effective.in_service_date == date(2020, 11, 20)


def test_two_sorters_of_one_organization_resolve_apart():
    central = resolve_efficiency_settings(ORGANIZATION, SorterEfficiencySettings(labor_rate=Decimal("25.00"), one_time_cost=Decimal("118003.92")))
    east = resolve_efficiency_settings(ORGANIZATION, SorterEfficiencySettings(manual_items_per_hour=Decimal("50.0")))

    assert (central.labor_rate.source, central.manual_items_per_hour.source) == ("sorter", "organization")
    assert (east.labor_rate.source, east.manual_items_per_hour.source) == ("organization", "sorter")
    assert east.one_time_cost is None


# =====================================================================================================================
# The module itself
# =====================================================================================================================

def test_every_model_is_immutable():
    for model in (
        OrganizationEfficiencySettings(),
        SorterEfficiencySettings(),
        ResolvedRate(Decimal("1.0"), "sorter"),
        resolve_efficiency_settings(OrganizationEfficiencySettings(), SorterEfficiencySettings()),
        EfficiencySettingProblem("labor_rate", "out_of_range"),
    ):
        field = dataclasses.fields(model)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(model, field, None)


def test_the_module_is_framework_neutral_and_reads_no_database():
    source = inspect.getsource(efficiency_settings)

    for forbidden in ("streamlit", "fastapi", "sqlalchemy", "pandas", "get_engine", "get_effective_settings", "os.getenv", "logging"):
        assert forbidden not in source.split('"""', 2)[2], forbidden
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == [
        "from __future__ import annotations",
        "import re",
        "from collections.abc import Mapping",
        "from dataclasses import dataclass",
        "from datetime import date",
        "from decimal import Decimal",
        "from typing import Any, Literal",
    ]


def test_the_sorter_fields_are_the_organizations_two_and_three_of_its_own():
    assert ORGANIZATION_FIELDS == ("labor_rate", "manual_items_per_hour")
    assert SORTER_FIELDS == ("labor_rate", "manual_items_per_hour", "one_time_cost", "recurring_annual_cost", "in_service_date")
    assert [field.name for field in dataclasses.fields(SorterEfficiencySettings)] == list(SORTER_FIELDS)
    assert EFFICIENCY_SETTINGS_KEY == "efficiency"
