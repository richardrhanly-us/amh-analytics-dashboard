"""Efficiency assumptions: what an organization and its sorter sites have
configured for the Efficiency reports, read from their settings documents.
Pure: no database, no clock, no request.

WHAT IS CONFIGURED. Five things, all of them the customer's own figures:

    labor_rate               what an hour of staff time costs
    manual_items_per_hour    how many items a person checks in and sorts by hand in an hour
    one_time_cost            what the sorter cost to buy and install
    recurring_annual_cost    what it costs to keep, per year
    in_service_date          the date it went into service

The time model is manual-equivalent only: nothing here is a machine's own
processing rate, and none is inferred.

WHERE IT IS STORED. Under the `efficiency` key of the two settings documents
that already exist, as text:

    organization_settings.settings_json["efficiency"]    labor_rate, manual_items_per_hour
    branch_settings.settings_json["efficiency"]          those two again, and the three about the sorter itself

A sorter site is one host branch (services.sorter_inventory_service), so the
branch's document is the sorter's.

WHAT INHERITS, AND WHAT MUST NOT. The two rates are the organization's
defaults, and a sorter may override either. The two costs and the date
belong to one sorter and are NEVER taken from the organization's document: a
cost written there by mistake, handed down to every sorter, would be counted
once per sorter. That is why this module reads the two documents apart and
resolves them itself (resolve_efficiency_settings). The dashboard's effective
settings (services.tenant_service.get_effective_settings) deep-merge the
whole organization document under the branch's, which would do exactly that;
they are not used for Efficiency.

UNKNOWN IS NOT ZERO. A field that is absent, or null, is unknown: None.
"0.00" is a cost someone said is zero. There is no built-in labor rate and no
built-in manual rate: what nobody entered is not known.

MONEY AND RATES ARE DECIMAL, WRITTEN AS TEXT. A value is a string of ASCII
digits with an optional decimal point and at most the field's number of
decimal places -- no sign, exponent, space, separator, currency symbol or
leading zero. A JSON number is refused, since it has already been through a
binary float. Nothing is ever rounded: a value with more places than the
field keeps is refused, and one with fewer is the same number, written out
in full when serialized ("17.5" is "17.50").

A MALFORMED VALUE IS AN ERROR, NOT A DEFAULT. EfficiencySettingsError names
each field at fault and why, by code, and quotes no stored value. Only code
that asks for Efficiency settings calls this module, so a bad `efficiency`
block can fail an Efficiency read and nothing else. Every other key of a
settings document is ignored, and no document is ever modified.

Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

# The key of the Efficiency block in a settings document. A write replaces
# the value at this one path and leaves the rest of the document alone.
EFFICIENCY_SETTINGS_KEY = "efficiency"

SettingSource = Literal["organization", "sorter"]

# Why a field was refused. Stable codes: safe to log and to hand to a client.
NOT_AN_OBJECT = "not_an_object"
NOT_A_STRING = "not_a_string"
NOT_A_DECIMAL = "not_a_decimal"
TOO_MANY_DECIMAL_PLACES = "too_many_decimal_places"
OUT_OF_RANGE = "out_of_range"
NOT_A_DATE = "not_a_date"
IN_THE_FUTURE = "in_the_future"
UNKNOWN_FIELD = "unknown_field"


@dataclass(frozen=True, slots=True)
class EfficiencySettingProblem:
    """One field that was refused, and why. `field` is the field's own name,
    or EFFICIENCY_SETTINGS_KEY when the block itself is at fault."""

    field: str
    code: str


class EfficiencySettingsError(ValueError):
    """An Efficiency block that cannot be used. `problems` has every field
    at fault, in the block's own field order. The message carries field
    names and codes only -- never a stored value."""

    def __init__(self, problems: tuple[EfficiencySettingProblem, ...]) -> None:
        self.problems = problems
        super().__init__("Invalid efficiency settings: " + ", ".join(f"{problem.field} ({problem.code})" for problem in problems))


# =====================================================================================================================
# Fields
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class _DecimalField:
    """A money or rate field: how many decimal places it keeps, and the
    values it may hold. `minimum` is allowed unless `above_minimum`."""

    places: int
    minimum: Decimal
    maximum: Decimal
    above_minimum: bool = False


_FIELDS: dict[str, _DecimalField] = {
    "labor_rate": _DecimalField(places=2, minimum=Decimal(0), maximum=Decimal(1000), above_minimum=True),
    "manual_items_per_hour": _DecimalField(places=1, minimum=Decimal(1), maximum=Decimal(1000)),
    "one_time_cost": _DecimalField(places=2, minimum=Decimal(0), maximum=Decimal(100_000_000)),
    "recurring_annual_cost": _DecimalField(places=2, minimum=Decimal(0), maximum=Decimal(10_000_000)),
}
_IN_SERVICE_DATE = "in_service_date"

# Which fields each document owns, in the order they are serialized.
ORGANIZATION_FIELDS: tuple[str, ...] = ("labor_rate", "manual_items_per_hour")
SORTER_FIELDS: tuple[str, ...] = ("labor_rate", "manual_items_per_hour", "one_time_cost", "recurring_annual_cost", _IN_SERVICE_DATE)

# ASCII digits only ([0-9], not \d, which also matches other scripts' digits), no leading zero, no sign.
_DECIMAL_TEXT = re.compile(r"(0|[1-9][0-9]*)(?:\.([0-9]+))?")
_DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Longer than any value a field can hold; anything longer is not looked at further.
_MAX_TEXT_LENGTH = 32


def _decimal(value: object, field: _DecimalField) -> Decimal | str:
    """The field's value, or the code for why `value` is not one."""
    if not isinstance(value, str):
        return NOT_A_STRING
    match = _DECIMAL_TEXT.fullmatch(value) if len(value) <= _MAX_TEXT_LENGTH else None
    if match is None:
        return NOT_A_DECIMAL
    if len(match.group(2) or "") > field.places:
        return TOO_MANY_DECIMAL_PLACES
    number = Decimal(value)
    below = number <= field.minimum if field.above_minimum else number < field.minimum
    if below or number > field.maximum:
        return OUT_OF_RANGE
    # Exact: the text has no more places than this, so nothing is rounded.
    return number.quantize(Decimal(1).scaleb(-field.places))


def _date(value: object, not_after: date | None) -> date | str:
    """A calendar date written YYYY-MM-DD, or the code for why `value` is
    not one. With `not_after`, a later date is refused."""
    if not isinstance(value, str):
        return NOT_A_STRING
    if _DATE_TEXT.fullmatch(value) is None:
        return NOT_A_DATE
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return NOT_A_DATE
    if not_after is not None and parsed > not_after:
        return IN_THE_FUTURE
    return parsed


def _fields(block: object, owned: tuple[str, ...], *, refuse_unknown: bool, not_after: date | None) -> dict[str, Any]:
    """The value of every field in `owned`, None where the block has none.
    Raises EfficiencySettingsError naming every field that is malformed."""
    if block is None:
        return dict.fromkeys(owned)
    if not isinstance(block, Mapping):
        raise EfficiencySettingsError((EfficiencySettingProblem(EFFICIENCY_SETTINGS_KEY, NOT_AN_OBJECT),))

    values: dict[str, Any] = {}
    problems: list[EfficiencySettingProblem] = []
    for name in owned:
        stored = block.get(name)
        if stored is None:
            values[name] = None
            continue
        parsed = _date(stored, not_after) if name == _IN_SERVICE_DATE else _decimal(stored, _FIELDS[name])
        if isinstance(parsed, str):
            problems.append(EfficiencySettingProblem(name, parsed))
        else:
            values[name] = parsed

    if refuse_unknown:
        problems.extend(EfficiencySettingProblem(str(name), UNKNOWN_FIELD) for name in block if name not in owned)
    if problems:
        raise EfficiencySettingsError(tuple(problems))
    return values


def _stored_block(settings_document: object) -> object:
    """The Efficiency block of a stored settings document: None when the
    document has none, or is not an object at all (nothing stored)."""
    return settings_document.get(EFFICIENCY_SETTINGS_KEY) if isinstance(settings_document, Mapping) else None


# =====================================================================================================================
# What each document says
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class OrganizationEfficiencySettings:
    """An organization's defaults. None is unknown."""

    labor_rate: Decimal | None = None
    manual_items_per_hour: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SorterEfficiencySettings:
    """What one sorter site's own document says. For the two rates None
    means "no override here"; for the costs and the date it means unknown --
    and a cost of Decimal("0.00") is a cost of zero, not an unknown one."""

    labor_rate: Decimal | None = None
    manual_items_per_hour: Decimal | None = None
    one_time_cost: Decimal | None = None
    recurring_annual_cost: Decimal | None = None
    in_service_date: date | None = None


def parse_organization_efficiency_settings(settings_document: object) -> OrganizationEfficiencySettings:
    """What a stored organization settings document says about Efficiency.

    Only the organization's two fields are read. Anything else in the block
    -- a sorter's cost written here by mistake included -- is not the
    organization's to say and is ignored, as is every other key of the
    document. Raises EfficiencySettingsError if either field is malformed.
    """
    return OrganizationEfficiencySettings(
        **_fields(_stored_block(settings_document), ORGANIZATION_FIELDS, refuse_unknown=False, not_after=None)
    )


def parse_sorter_efficiency_settings(settings_document: object) -> SorterEfficiencySettings:
    """What a stored sorter-site (branch) settings document says about
    Efficiency. Keys this module does not know are ignored. Raises
    EfficiencySettingsError if any field is malformed.

    A stored in_service_date is not checked against today: it was checked
    when it was written (validate_sorter_efficiency_settings), and reading
    it must not depend on a clock.
    """
    return SorterEfficiencySettings(**_fields(_stored_block(settings_document), SORTER_FIELDS, refuse_unknown=False, not_after=None))


# =====================================================================================================================
# What someone is about to store
# =====================================================================================================================

def validate_organization_efficiency_settings(block: object) -> OrganizationEfficiencySettings:
    """An Efficiency block someone wants to store for an organization --
    the block itself, not a whole settings document. Stricter than reading:
    a key that is not one of the organization's two fields is refused
    (UNKNOWN_FIELD), so a sorter's cost cannot be stored where it would mean
    nothing. A null or absent field is "not set"."""
    return OrganizationEfficiencySettings(**_fields(block, ORGANIZATION_FIELDS, refuse_unknown=True, not_after=None))


def validate_sorter_efficiency_settings(block: object, *, today: date) -> SorterEfficiencySettings:
    """An Efficiency block someone wants to store for a sorter site. Unknown
    keys are refused, and in_service_date may not be after `today` -- the
    product's current date, which the caller supplies: nothing here reads a
    clock."""
    return SorterEfficiencySettings(**_fields(block, SORTER_FIELDS, refuse_unknown=True, not_after=today))


def _serialized(settings: OrganizationEfficiencySettings | SorterEfficiencySettings, owned: tuple[str, ...]) -> dict[str, str]:
    block: dict[str, str] = {}
    for name in owned:
        value = getattr(settings, name)
        if value is None:
            continue
        # A date as YYYY-MM-DD; a Decimal with exactly its field's places, never in exponent form.
        block[name] = value.isoformat() if isinstance(value, date) else f"{value:.{_FIELDS[name].places}f}"
    return block


def serialize_organization_efficiency_settings(settings: OrganizationEfficiencySettings) -> dict[str, str]:
    """The Efficiency block to store for an organization: its fields that
    are set, as canonical text. The value for the document's
    EFFICIENCY_SETTINGS_KEY, and nothing else of the document."""
    return _serialized(settings, ORGANIZATION_FIELDS)


def serialize_sorter_efficiency_settings(settings: SorterEfficiencySettings) -> dict[str, str]:
    """The Efficiency block to store for a sorter site. An unknown field is
    left out; a cost of zero is written as "0.00"."""
    return _serialized(settings, SORTER_FIELDS)


# =====================================================================================================================
# What applies to one sorter
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class ResolvedRate:
    """A rate that applies to a sorter, and whose it is: the sorter's own
    override, or the organization's default."""

    value: Decimal
    source: SettingSource


@dataclass(frozen=True, slots=True)
class EffectiveEfficiencySettings:
    """The assumptions that apply to one sorter site. None is unknown,
    throughout. The costs and the date are the sorter's own or nothing."""

    labor_rate: ResolvedRate | None
    manual_items_per_hour: ResolvedRate | None
    one_time_cost: Decimal | None
    recurring_annual_cost: Decimal | None
    in_service_date: date | None


def _rate(sorter: Decimal | None, organization: Decimal | None) -> ResolvedRate | None:
    if sorter is not None:
        return ResolvedRate(sorter, "sorter")
    return ResolvedRate(organization, "organization") if organization is not None else None


def resolve_efficiency_settings(
    organization: OrganizationEfficiencySettings, sorter: SorterEfficiencySettings
) -> EffectiveEfficiencySettings:
    """The assumptions for one sorter: each rate is the sorter's own if it
    has one, else the organization's, else unknown. The two costs and the
    date are the sorter's alone -- OrganizationEfficiencySettings has no
    such fields to take them from."""
    return EffectiveEfficiencySettings(
        labor_rate=_rate(sorter.labor_rate, organization.labor_rate),
        manual_items_per_hour=_rate(sorter.manual_items_per_hour, organization.manual_items_per_hour),
        one_time_cost=sorter.one_time_cost,
        recurring_annual_cost=sorter.recurring_annual_cost,
        in_service_date=sorter.in_service_date,
    )
