"""SortView Value: what an organization says its reporting and troubleshooting
work took before SortView and takes now, and the estimate made from that.
Pure: no database, no clock, no request.

    parse_sortview_value_settings(settings_document)        -> SortViewValueSettings
    validate_sortview_value_settings(block, today=...)      -> SortViewValueSettings
    serialize_sortview_value_settings(settings)             -> dict
    resolve_labor_rate(settings, organization_efficiency)   -> ResolvedLaborRate | None
    calculate_sortview_value(settings, labor_rate)          -> SortViewValueReport

WHAT IS ESTIMATED. The staff time two kinds of work take in a month, before
and now, and what the difference is worth beside what SortView costs:

    reporting        tasks_per_month * minutes_per_task / 60
    troubleshooting  incidents_per_month * staff_per_incident * minutes / 60

    recovered_hours_per_month   baseline hours - current hours
    monthly_value               recovered_hours_per_month * labor_rate
    annual_value                monthly_value * 12

    for the organization, both kinds of work together:
    net_annual_value            annual_value - subscription_annual_cost
    roi_percent                 net_annual_value / subscription_annual_cost * 100
    payback_months              subscription_annual_cost / monthly_value

`payback_months` is how many months of the estimated monthly value it takes to
cover a year of the subscription. It is not a date, and nothing is paid back.

Every figure is the organization's own assumption worked through: nothing here
is measured by SortView, taken from a sorter's volume, or an industry figure,
and none of it says staff time was removed from anyone's schedule.

WHAT IS CONFIGURED, AND WHERE. One block, under the `sortview_value` key of
the organization's settings document, as text:

    labor_rate                 what an hour of this staff time costs
    subscription_annual_cost   what SortView costs the organization in a year
    reporting                  tasks_per_month, baseline_minutes_per_task, current_minutes_per_task
    troubleshooting            incidents_per_month, staff_per_incident, baseline_minutes, current_minutes

Each of the two kinds of work also says what its figures are -- `basis`, an
`estimate` or `measured` -- and the date they are as of, `as_of`. Those two
describe the figures. They change none of them.

THE LABOR RATE is this block's own if it has one, else the organization's
Efficiency default (services.efficiency_settings), else unknown; and which of
the two it was is kept with it. There is one rate, for both kinds of work.

UNKNOWN IS NOT ZERO. A field that is absent, or null, is unknown: None. "0.0"
is a figure someone entered. A figure whose inputs are not all known is None;
the others are still worked out, and `missing` names every input the estimate
uses that nobody has set. There is no built-in rate, volume or duration.

THE TWO KINDS OF WORK STAND APART, AND THE TOTAL NEEDS BOTH. Each has its own
figures as soon as its own inputs are known. The organization's figures --
hours, value, net, return, payback -- exist only when both do: one kind of
work is never passed off as the whole.

NOTHING IS CLIPPED. Work that takes longer now than it did gives negative
hours, a negative value and a negative return, as they are. Two figures do
not exist rather than mislead: a return on a cost of zero, and a payback when
the monthly value is zero or less.

MONEY, RATES AND COUNTS ARE DECIMAL, WRITTEN AS TEXT, by the rules of
services.efficiency_settings -- its own checks are used as they are: ASCII
digits with an optional decimal point and at most the field's places, no
sign, exponent, space, separator or symbol; a JSON number is refused. Nothing
is rounded on the way in.

EXACT, THEN ROUNDED ONCE. Everything is carried at far more digits than any
result keeps, and each figure is rounded once, half up, from its own exact
value: hours and money to 0.01, the return and the payback to 0.1. No figure
is made from another's rounded value, so no rounding is compounded -- which
also means figures as reported can differ from their own sum by a cent.

A MALFORMED VALUE IS AN ERROR, NOT A DEFAULT. SortViewValueError names each
field at fault and why, by code, and quotes no stored or submitted value. A
field of a kind of work is named with it: `reporting.tasks_per_month`.

Standard library and services.efficiency_settings only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any, Literal

from services.efficiency_settings import (
    NOT_A_STRING,
    NOT_AN_OBJECT,
    UNKNOWN_FIELD,
    DecimalField,
    OrganizationEfficiencySettings,
    parse_date_text,
    parse_decimal_text,
)

# The key of the block in an organization's settings document.
SORTVIEW_VALUE_SETTINGS_KEY = "sortview_value"

# The one code this module adds to services.efficiency_settings' own: a basis that is not one of BASES.
NOT_A_BASIS = "not_a_basis"

Basis = Literal["estimate", "measured"]
BASES: tuple[Basis, ...] = ("estimate", "measured")

LaborRateSource = Literal["sortview_value", "organization_efficiency"]

REPORTING = "reporting"
TROUBLESHOOTING = "troubleshooting"

MINUTES_PER_HOUR = 60
MONTHS_PER_YEAR = 12

HOURS_PLACES = Decimal("0.01")
MONEY_PLACES = Decimal("0.01")
ROI_PLACES = Decimal("0.1")
PAYBACK_PLACES = Decimal("0.1")

# Far more digits than the largest value times the smallest step needs, and independent of whatever the process's
# own decimal context happens to be.
_EXACT = Context(prec=60, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class SortViewValueProblem:
    """One field that was refused, and why. `field` is the field's own name --
    with its kind of work in front where it has one -- or
    SORTVIEW_VALUE_SETTINGS_KEY when the block itself is at fault."""

    field: str
    code: str


class SortViewValueError(ValueError):
    """A SortView Value block that cannot be used. `problems` has every field
    at fault, in the block's own field order. The message carries field names
    and codes only -- never a stored or submitted value."""

    def __init__(self, problems: tuple[SortViewValueProblem, ...]) -> None:
        self.problems = problems
        super().__init__("Invalid SortView Value settings: " + ", ".join(f"{problem.field} ({problem.code})" for problem in problems))


# =====================================================================================================================
# Fields
# =====================================================================================================================

# How often something happens in a month, and how long it takes. Generous on purpose: these keep out a slip of the
# keyboard, not an unusual library. Zero is a figure -- no tasks, or a task that now takes no time at all.
_PER_MONTH = DecimalField(places=1, minimum=Decimal(0), maximum=Decimal(100_000))
_MINUTES = DecimalField(places=1, minimum=Decimal(0), maximum=Decimal(10_000))

# The two fields of the block itself. An hour of staff time costs something, as for Efficiency; a subscription can
# cost nothing.
_FIELDS: dict[str, DecimalField] = {
    "labor_rate": DecimalField(places=2, minimum=Decimal(0), maximum=Decimal(10_000), above_minimum=True),
    "subscription_annual_cost": DecimalField(places=2, minimum=Decimal(0), maximum=Decimal(10_000_000)),
}

# The figures of each kind of work, in the order they are serialized. An incident nobody attends is not an incident:
# staff_per_incident is above zero.
_CATEGORY_FIELDS: dict[str, dict[str, DecimalField]] = {
    REPORTING: {
        "tasks_per_month": _PER_MONTH,
        "baseline_minutes_per_task": _MINUTES,
        "current_minutes_per_task": _MINUTES,
    },
    TROUBLESHOOTING: {
        "incidents_per_month": _PER_MONTH,
        "staff_per_incident": DecimalField(places=1, minimum=Decimal(0), maximum=Decimal(1_000), above_minimum=True),
        "baseline_minutes": _MINUTES,
        "current_minutes": _MINUTES,
    },
}
_BASIS = "basis"
_AS_OF = "as_of"

# Every input the estimate uses, in the order `missing` lists them. A kind of work's basis and date are not among
# them: nothing is worked out from either.
USED_INPUTS: tuple[str, ...] = (
    *_FIELDS,
    *(f"{category}.{name}" for category, fields in _CATEGORY_FIELDS.items() for name in fields),
)


# =====================================================================================================================
# What the block says
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class ReportingInputs:
    """Reporting and statistics work: how many tasks a month, and how long
    one took before SortView and takes now. None is unknown."""

    tasks_per_month: Decimal | None = None
    baseline_minutes_per_task: Decimal | None = None
    current_minutes_per_task: Decimal | None = None
    basis: Basis | None = None
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class TroubleshootingInputs:
    """Troubleshooting work: how many incidents a month, how many staff one
    involves, and how long each of them spent on one before SortView and
    spends now. None is unknown."""

    incidents_per_month: Decimal | None = None
    staff_per_incident: Decimal | None = None
    baseline_minutes: Decimal | None = None
    current_minutes: Decimal | None = None
    basis: Basis | None = None
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class SortViewValueSettings:
    """What an organization's document says about SortView Value. None is
    unknown, throughout -- and a cost of Decimal("0.00") is a cost of zero,
    not an unknown one. For `labor_rate` None means "no rate of its own
    here": resolve_labor_rate says what applies."""

    labor_rate: Decimal | None = None
    subscription_annual_cost: Decimal | None = None
    reporting: ReportingInputs = ReportingInputs()
    troubleshooting: TroubleshootingInputs = TroubleshootingInputs()


def _category(
    block: object, category: str, *, refuse_unknown: bool, not_after: date | None, problems: list[SortViewValueProblem]
) -> dict[str, Any]:
    """The value of every field of one kind of work, None where the block has
    none or the value was refused. Every field at fault is added to
    `problems`, named with its kind of work."""
    fields = _CATEGORY_FIELDS[category]
    owned = (*fields, _BASIS, _AS_OF)
    values: dict[str, Any] = dict.fromkeys(owned)
    if block is None:
        return values
    if not isinstance(block, Mapping):
        problems.append(SortViewValueProblem(category, NOT_AN_OBJECT))
        return values

    for name in owned:
        stored = block.get(name)
        if stored is None:
            continue
        if name == _BASIS:
            if not isinstance(stored, str):
                problems.append(SortViewValueProblem(f"{category}.{name}", NOT_A_STRING))
            elif stored not in BASES:
                problems.append(SortViewValueProblem(f"{category}.{name}", NOT_A_BASIS))
            else:
                values[name] = stored
            continue
        parsed = parse_date_text(stored, not_after) if name == _AS_OF else parse_decimal_text(stored, fields[name])
        if isinstance(parsed, str):
            problems.append(SortViewValueProblem(f"{category}.{name}", parsed))
        else:
            values[name] = parsed

    if refuse_unknown:
        problems.extend(SortViewValueProblem(f"{category}.{name}", UNKNOWN_FIELD) for name in block if name not in owned)
    return values


def _settings(block: object, *, refuse_unknown: bool, not_after: date | None) -> SortViewValueSettings:
    """What a SortView Value block says. Raises SortViewValueError naming
    every field that is malformed, in the block's own field order."""
    if block is None:
        return SortViewValueSettings()
    if not isinstance(block, Mapping):
        raise SortViewValueError((SortViewValueProblem(SORTVIEW_VALUE_SETTINGS_KEY, NOT_AN_OBJECT),))

    problems: list[SortViewValueProblem] = []
    values: dict[str, Any] = {}
    for name, field in _FIELDS.items():
        stored = block.get(name)
        parsed = None if stored is None else parse_decimal_text(stored, field)
        if isinstance(parsed, str):
            problems.append(SortViewValueProblem(name, parsed))
            parsed = None
        values[name] = parsed

    reporting = _category(block.get(REPORTING), REPORTING, refuse_unknown=refuse_unknown, not_after=not_after, problems=problems)
    troubleshooting = _category(
        block.get(TROUBLESHOOTING), TROUBLESHOOTING, refuse_unknown=refuse_unknown, not_after=not_after, problems=problems
    )

    if refuse_unknown:
        owned = (*_FIELDS, REPORTING, TROUBLESHOOTING)
        problems.extend(SortViewValueProblem(str(name), UNKNOWN_FIELD) for name in block if name not in owned)
    if problems:
        raise SortViewValueError(tuple(problems))
    return SortViewValueSettings(
        **values, reporting=ReportingInputs(**reporting), troubleshooting=TroubleshootingInputs(**troubleshooting)
    )


def parse_sortview_value_settings(settings_document: object) -> SortViewValueSettings:
    """What a stored organization settings document says about SortView
    Value. A document with no such block, or that is not an object at all,
    says nothing: every field unknown. Keys this module does not know are
    ignored, as is every other key of the document. Raises SortViewValueError
    if any field is malformed.

    A stored `as_of` is not checked against today: it was checked when it was
    written (validate_sortview_value_settings), and reading it must not
    depend on a clock.
    """
    block = settings_document.get(SORTVIEW_VALUE_SETTINGS_KEY) if isinstance(settings_document, Mapping) else None
    return _settings(block, refuse_unknown=False, not_after=None)


def validate_sortview_value_settings(block: object, *, today: date) -> SortViewValueSettings:
    """A SortView Value block someone wants to store -- the block itself, not
    a whole settings document. Stricter than reading: a key that is not one
    of its fields is refused (UNKNOWN_FIELD), and neither `as_of` may be after
    `today` -- the product's current date, which the caller supplies: nothing
    here reads a clock. A null or absent field is "not set"."""
    return _settings(block, refuse_unknown=True, not_after=today)


def _serialized(inputs: object, fields: Mapping[str, DecimalField], names: tuple[str, ...]) -> dict[str, str]:
    block: dict[str, str] = {}
    for name in names:
        value = getattr(inputs, name)
        if value is None:
            continue
        if isinstance(value, Decimal):
            # Exactly its field's places, never in exponent form.
            block[name] = f"{value:.{fields[name].places}f}"
        else:
            block[name] = value.isoformat() if isinstance(value, date) else value
    return block


def serialize_sortview_value_settings(settings: SortViewValueSettings) -> dict[str, Any]:
    """The block to store: the fields that are set, as canonical text, each
    kind of work under its own key. An unknown field is left out, and so is a
    kind of work with nothing set; a figure of zero is written out ("0.0").
    The value for the document's SORTVIEW_VALUE_SETTINGS_KEY, and nothing else
    of the document."""
    block: dict[str, Any] = dict(_serialized(settings, _FIELDS, tuple(_FIELDS)))
    for category, inputs in ((REPORTING, settings.reporting), (TROUBLESHOOTING, settings.troubleshooting)):
        fields = _CATEGORY_FIELDS[category]
        written = _serialized(inputs, fields, (*fields, _BASIS, _AS_OF))
        if written:
            block[category] = written
    return block


# =====================================================================================================================
# What an hour of the time is worth
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class ResolvedLaborRate:
    """The labor rate that applies, and whose it is: the SortView Value
    block's own, or the organization's Efficiency default."""

    value: Decimal
    source: LaborRateSource


def resolve_labor_rate(
    settings: SortViewValueSettings, organization_efficiency: OrganizationEfficiencySettings
) -> ResolvedLaborRate | None:
    """The one labor rate for both kinds of work: the block's own if it has
    one, else the organization's Efficiency default, else unknown. Nothing
    else of Efficiency is used, and no sorter's override ever is."""
    if settings.labor_rate is not None:
        return ResolvedLaborRate(settings.labor_rate, "sortview_value")
    if organization_efficiency.labor_rate is not None:
        return ResolvedLaborRate(organization_efficiency.labor_rate, "organization_efficiency")
    return None


# =====================================================================================================================
# The estimate
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class CategoryValue:
    """One kind of work in a month, before and now, and what the difference
    is worth. Each Decimal is already rounded to the places it is reported
    in; None is a figure that could not be worked out from what is known.
    `recovered_hours_per_month`, and so both values, may be negative."""

    baseline_hours_per_month: Decimal | None
    current_hours_per_month: Decimal | None
    recovered_hours_per_month: Decimal | None
    monthly_value: Decimal | None
    annual_value: Decimal | None


@dataclass(frozen=True, slots=True)
class SortViewValueReport:
    """The estimate. `reporting` and `troubleshooting` are each kind of work
    by itself. The figures beside them are the organization's, and are None
    unless BOTH kinds of work have theirs. `payback_months` is the months of
    `monthly_value` it takes to cover the annual subscription cost. `missing`
    names every input the estimate uses that has not been set, in a fixed
    order."""

    reporting: CategoryValue
    troubleshooting: CategoryValue
    recovered_hours_per_month: Decimal | None
    monthly_value: Decimal | None
    annual_value: Decimal | None
    net_annual_value: Decimal | None
    roi_percent: Decimal | None
    payback_months: Decimal | None
    missing: tuple[str, ...]


def _rounded(value: Decimal | None, places: Decimal) -> Decimal | None:
    """`value` to `places`, half up -- and a zero is zero, never "-0.00"."""
    if value is None:
        return None
    rounded = _EXACT.quantize(value, places)
    return rounded.copy_abs() if rounded.is_zero() else rounded


def _product(*factors: Decimal | None) -> Decimal | None:
    """The factors multiplied together, exactly -- or None if any is unknown."""
    result = Decimal(1)
    for factor in factors:
        if factor is None:
            return None
        result = _EXACT.multiply(result, factor)
    return result


def _sum(first: Decimal | None, second: Decimal | None) -> Decimal | None:
    return None if first is None or second is None else _EXACT.add(first, second)


@dataclass(frozen=True, slots=True)
class _Exact:
    """One kind of work, unrounded: what every reported figure is made from."""

    baseline_hours: Decimal | None
    current_hours: Decimal | None
    recovered_hours: Decimal | None
    monthly_value: Decimal | None

    def reported(self) -> CategoryValue:
        return CategoryValue(
            baseline_hours_per_month=_rounded(self.baseline_hours, HOURS_PLACES),
            current_hours_per_month=_rounded(self.current_hours, HOURS_PLACES),
            recovered_hours_per_month=_rounded(self.recovered_hours, HOURS_PLACES),
            monthly_value=_rounded(self.monthly_value, MONEY_PLACES),
            annual_value=_rounded(_product(self.monthly_value, Decimal(MONTHS_PER_YEAR)), MONEY_PLACES),
        )


def _category_value(
    volume: Decimal | None, baseline_minutes: Decimal | None, current_minutes: Decimal | None, labor_rate: Decimal | None
) -> _Exact:
    """`volume` is how many times a month the work is done by one person:
    tasks, or incidents times the staff each involves."""

    def hours(minutes: Decimal | None) -> Decimal | None:
        total = _product(volume, minutes)
        return None if total is None else _EXACT.divide(total, Decimal(MINUTES_PER_HOUR))

    baseline = hours(baseline_minutes)
    current = hours(current_minutes)
    recovered = None if baseline is None or current is None else _EXACT.subtract(baseline, current)
    return _Exact(baseline, current, recovered, _product(recovered, labor_rate))


def calculate_sortview_value(settings: SortViewValueSettings, labor_rate: ResolvedLaborRate | None) -> SortViewValueReport:
    """The estimate from what is configured. `labor_rate` is the rate that
    applies (resolve_labor_rate), or None when there is none.

    Each kind of work is worked out from its own inputs. The organization's
    figures are worked out only when both kinds have theirs, from the exact
    values and never from the rounded ones.
    """
    rate = None if labor_rate is None else labor_rate.value
    cost = settings.subscription_annual_cost

    reporting = _category_value(
        settings.reporting.tasks_per_month,
        settings.reporting.baseline_minutes_per_task,
        settings.reporting.current_minutes_per_task,
        rate,
    )
    troubleshooting = _category_value(
        _product(settings.troubleshooting.incidents_per_month, settings.troubleshooting.staff_per_incident),
        settings.troubleshooting.baseline_minutes,
        settings.troubleshooting.current_minutes,
        rate,
    )

    # Both, or nothing: _sum is None unless each kind of work has the figure.
    recovered_hours = _sum(reporting.recovered_hours, troubleshooting.recovered_hours)
    monthly_value = _sum(reporting.monthly_value, troubleshooting.monthly_value)
    annual_value = _product(monthly_value, Decimal(MONTHS_PER_YEAR))
    net_annual_value = None if annual_value is None or cost is None else _EXACT.subtract(annual_value, cost)

    # A return on a cost of nothing does not exist; nor does a payback from a value that never covers the cost.
    roi_percent = (
        None
        if net_annual_value is None or cost is None or cost.is_zero()
        else _EXACT.multiply(_EXACT.divide(net_annual_value, cost), Decimal(100))
    )
    payback_months = None if monthly_value is None or cost is None or monthly_value <= 0 else _EXACT.divide(cost, monthly_value)

    known: dict[str, object] = {
        "labor_rate": rate,
        "subscription_annual_cost": cost,
        **{f"{REPORTING}.{name}": getattr(settings.reporting, name) for name in _CATEGORY_FIELDS[REPORTING]},
        **{f"{TROUBLESHOOTING}.{name}": getattr(settings.troubleshooting, name) for name in _CATEGORY_FIELDS[TROUBLESHOOTING]},
    }
    return SortViewValueReport(
        reporting=reporting.reported(),
        troubleshooting=troubleshooting.reported(),
        recovered_hours_per_month=_rounded(recovered_hours, HOURS_PLACES),
        monthly_value=_rounded(monthly_value, MONEY_PLACES),
        annual_value=_rounded(annual_value, MONEY_PLACES),
        net_annual_value=_rounded(net_annual_value, MONEY_PLACES),
        roi_percent=_rounded(roi_percent, ROI_PLACES),
        payback_months=_rounded(payback_months, PAYBACK_PLACES),
        missing=tuple(name for name in USED_INPUTS if known[name] is None),
    )
