"""Request and response models for the customer API's SortView Value settings routes.

Every figure is TEXT: a decimal written out in full ("32.50", "12.0",
"0.00"), or a calendar date (YYYY-MM-DD). None is a JSON number, so none has
been through a binary float. `null` is "not set" -- and never the same thing
as "0.00", which is a figure someone entered.

These are the SETTINGS: what an organization has entered, and which labor
rate applies. No figure worked out from them -- hours, value, return or
payback -- has a field here.

No organization, customer, branch, installation or user id has a field here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SortViewValueRequest(BaseModel):
    """The body of a PUT: the whole SortView Value block.

    It says which fields the block has and decides nothing about them. Each
    is taken exactly as it arrives (`Any`: nothing is converted, so a JSON
    number stays a number, and a kind of work that is not an object stays
    what it is, for the settings model to refuse), and a key that is not a
    field is kept (`extra="allow"`) so the settings model can name it. Every
    rule is services.sortview_value's. A field left out is the same as null:
    not set."""

    model_config = ConfigDict(extra="allow")

    labor_rate: Any = None
    subscription_annual_cost: Any = None
    reporting: Any = None
    troubleshooting: Any = None

    def block(self) -> dict[str, Any]:
        return self.model_dump()


class ValueLaborRate(_ResponseModel):
    """The labor rate, three ways: the SortView Value block's own, the
    organization's Efficiency default, and which of them applies.
    `effective` is the block's own if it has one, else the Efficiency
    default, else null -- and `source` says which, or is null when there is
    neither. A sorter's own Efficiency rate is never either."""

    sortview_value: str | None
    organization_efficiency: str | None
    effective: str | None
    source: Literal["sortview_value", "organization_efficiency"] | None


class ReportingSettings(_ResponseModel):
    """Reporting work, exactly as stored. Nothing is filled in."""

    tasks_per_month: str | None
    baseline_minutes_per_task: str | None
    current_minutes_per_task: str | None
    basis: Literal["estimate", "measured"] | None
    as_of: str | None


class TroubleshootingSettings(_ResponseModel):
    """Troubleshooting work, exactly as stored. Nothing is filled in."""

    incidents_per_month: str | None
    staff_per_incident: str | None
    baseline_minutes: str | None
    current_minutes: str | None
    basis: Literal["estimate", "measured"] | None
    as_of: str | None


class SortViewValue(_ResponseModel):
    """An organization's SortView Value settings. Every field is always
    there: null where nothing is set, a kind of work with nothing set
    included."""

    labor_rate: ValueLaborRate
    subscription_annual_cost: str | None
    reporting: ReportingSettings
    troubleshooting: TroubleshootingSettings


class SortViewValueSettingsResponse(_ResponseModel):
    sortview_value: SortViewValue
