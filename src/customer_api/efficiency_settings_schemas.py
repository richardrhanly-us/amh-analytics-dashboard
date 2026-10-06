"""Request and response models for the customer API's Efficiency settings routes.

Every figure is TEXT: a decimal written out in full ("17.56", "45.0",
"0.00"), or a calendar date (YYYY-MM-DD). None is a JSON number, so none has
been through a binary float. `null` is "not set" -- and never the same thing
as "0.00", which is a cost someone said is zero.

No organization, customer, branch, installation or user id has a field here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BlockRequest(BaseModel):
    """The body of a PUT: the whole Efficiency block for one level.

    It says which fields the block has and decides nothing about them. Each
    is taken exactly as it arrives (`Any`: nothing is converted, so a JSON
    number stays a number for the settings model to refuse), and a key that
    is not a field is kept (`extra="allow"`) so the settings model can name
    it. Every rule is services.efficiency_settings'. A field left out is the
    same as null: not set."""

    model_config = ConfigDict(extra="allow")

    def block(self) -> dict[str, Any]:
        return self.model_dump()


class OrganizationEfficiencyRequest(_BlockRequest):
    labor_rate: Any = None
    manual_items_per_hour: Any = None


class SorterEfficiencyRequest(_BlockRequest):
    labor_rate: Any = None
    manual_items_per_hour: Any = None
    one_time_cost: Any = None
    recurring_annual_cost: Any = None
    in_service_date: Any = None


class OrganizationEfficiency(_ResponseModel):
    """An organization's defaults, exactly as stored. Nothing is filled in:
    a rate nobody entered is null."""

    labor_rate: str | None
    manual_items_per_hour: str | None


class OrganizationEfficiencyResponse(_ResponseModel):
    efficiency: OrganizationEfficiency


class EfficiencyRate(_ResponseModel):
    """One rate, for one sorter: the organization's default, the sorter's own
    override, and which of them applies. `effective` is the sorter's if it
    has one, else the organization's, else null -- and `source` says which,
    or is null when there is neither."""

    organization: str | None
    sorter: str | None
    effective: str | None
    source: Literal["organization", "sorter"] | None


class SorterEfficiency(_ResponseModel):
    """A sorter site's Efficiency settings. The two rates can come from the
    organization. The two costs and the date are the sorter's own or null:
    they are never the organization's."""

    labor_rate: EfficiencyRate
    manual_items_per_hour: EfficiencyRate
    one_time_cost: str | None
    recurring_annual_cost: str | None
    in_service_date: str | None


class SorterEfficiencyResponse(_ResponseModel):
    efficiency: SorterEfficiency
