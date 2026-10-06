"""Response models for the customer API's sorter Efficiency report.

What was ASSUMED and what was WORKED OUT are kept apart: `assumptions` is
what the organization and the sorter have configured, `results` is the
arithmetic done with it (services.efficiency_report).

A count is an integer. Every hour, amount of money and rate is TEXT -- a
decimal written out in full ("69.78", "0.2199", "-103.36") -- never a JSON
number, so none has been through a binary float. A date is YYYY-MM-DD.
`null` is "not known": an assumption nobody set, or a result that cannot be
worked out without one. It is never the same as zero.

There is no annualized figure, payback, return or since-installation figure
here, and no organization, customer, branch, installation or user id.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from customer_api.report_schemas import ReportRange


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssumedRate(_ResponseModel):
    """A rate that applies to the sorter, and whose it is: the sorter's own
    override or the organization's default."""

    value: str
    source: Literal["organization", "sorter"]


class EfficiencyAssumptions(_ResponseModel):
    """What the figures were worked out from. The two rates are null when
    neither the sorter nor the organization has one. The two costs and the
    date are the sorter's own or null. `one_time_cost` is shown and used by
    nothing in this report."""

    manual_items_per_hour: AssumedRate | None
    labor_rate: AssumedRate | None
    recurring_annual_cost: str | None
    one_time_cost: str | None
    in_service_date: str | None


class EfficiencyResults(_ResponseModel):
    """The estimates. `in_service_days` are the days of the range on or
    after the sorter's in-service date (every day of it when there is no
    such date) and `in_service_checkin_count` the check-ins on those days:
    the days, and the check-ins, every figure below is about.

    `staff_time_equivalent_hours` is the staff time it would take to handle
    that many items by hand -- a manual equivalent, not time saved.
    `net_operational_value` is `labor_value_equivalent` less
    `recurring_cost`, exactly, and may be negative."""

    in_service_days: int
    in_service_checkin_count: int
    staff_time_equivalent_hours: str | None
    labor_value_equivalent: str | None
    recurring_cost: str | None
    net_operational_value: str | None
    recurring_cost_per_item: str | None


class EfficiencyReportResponse(_ResponseModel):
    """A sorter site's Efficiency report over a range. `checkin_count` is
    every check-in of the range -- processing events, the same count the
    sorter's other reports give. `missing` names each assumption this report
    uses that has not been set, in a fixed order."""

    range: ReportRange
    currency: Literal["USD"]
    checkin_count: int
    assumptions: EfficiencyAssumptions
    results: EfficiencyResults
    missing: list[Literal["manual_items_per_hour", "labor_rate", "recurring_annual_cost", "in_service_date"]]
