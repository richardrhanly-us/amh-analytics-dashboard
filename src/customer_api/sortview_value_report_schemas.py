"""Response models for the customer API's SortView Value report.

What was ASSUMED and what was WORKED OUT are kept apart: `assumptions` is
what the organization has entered and which labor rate applies -- the very
model the SortView Value settings routes answer with
(customer_api.sortview_value_schemas.SortViewValue) -- and `results` is the
arithmetic done with it (services.sortview_value).

Every hour, amount of money, percentage and number of months is TEXT -- a
decimal written out in full with a fixed number of places ("18.33",
"595.83", "-121.7", "8.1") -- never a JSON number, so none has been through a
binary float. `null` is "not known": an assumption nobody set, or a result
that cannot be worked out without one. It is never the same as zero. A
result may be negative.

There is no date range here: nothing in this report is counted from a
sorter's events. And there is no organization, customer, branch,
installation or user id.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from customer_api.sortview_value_schemas import SortViewValue


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryResults(_ResponseModel):
    """One kind of work in a month, before and now, and what the difference
    is worth. Each is there as soon as its own inputs are; the two values
    also need a labor rate. `recovered_hours_per_month`, and so both values,
    may be negative: work that takes longer now than it did."""

    baseline_hours_per_month: str | None
    current_hours_per_month: str | None
    recovered_hours_per_month: str | None
    monthly_value: str | None
    annual_value: str | None


class SortViewValueResults(_ResponseModel):
    """The estimate. `reporting` and `troubleshooting` are each kind of work
    by itself. The six figures beside them are the organization's, and are
    null unless BOTH kinds of work have theirs: one is never passed off as
    the whole.

    `net_annual_value` is `annual_value` less the annual subscription cost.
    `roi_percent` is that net over the cost, in percent: null when the cost
    is not known or is zero. `payback_months` is how many months of
    `monthly_value` it takes to cover a year of the subscription: null when
    the cost is not known, or the monthly value is zero or less."""

    reporting: CategoryResults
    troubleshooting: CategoryResults
    recovered_hours_per_month: str | None
    monthly_value: str | None
    annual_value: str | None
    net_annual_value: str | None
    roi_percent: str | None
    payback_months: str | None


MissingInput = Literal[
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


class SortViewValueReportResponse(_ResponseModel):
    """An organization's SortView Value report. `missing` names each input
    the estimate uses that has not been set, in a fixed order; `labor_rate`
    is among them only when neither the SortView Value settings nor the
    organization's Efficiency defaults have one."""

    currency: Literal["USD"]
    assumptions: SortViewValue
    results: SortViewValueResults
    missing: list[MissingInput]
