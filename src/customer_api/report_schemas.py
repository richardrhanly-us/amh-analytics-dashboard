"""Response models for the customer API's sorter-site range reports.

Every figure here is an INTEGER COUNT. No rate, average, percentage or
"busiest" value has a field: those are arithmetic on these counts and are
worked out by whoever shows them, from the counts and the range's `days`.

Every list is FULL LENGTH, with explicit zeros: one entry per calendar date
of the range, 24 hours, every reject reason, every destination enabled in
the site's settings. A range with no activity at all is a complete answer
made of zeros, never an empty one. The one list that is not full length is
the bin report's `bins`: which bins a sorter has is not known, so only the
bins that were observed are listed.

As everywhere in the customer API, the rows these are built from sit next to
identifiers and stored text that never leave the server -- tenant ids, item
and event keys, stored destination values, reject messages -- and none of
them has a field here.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from customer_api.operational_schemas import RoutingDestination, RoutingHome
from services.reject_reason import RejectReason


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportRange(_ResponseModel):
    """The range a report covers. `from` and `to` are the calendar dates that
    were asked for, both included, in the IANA zone `timezone` -- the
    product's zone, in which a day is a day. `days` is how many calendar
    dates that is. `includes_today` says the range reaches the product's
    current date, whose counts are therefore not final yet."""

    from_date: dt.date = Field(serialization_alias="from")
    to_date: dt.date = Field(serialization_alias="to")
    days: int
    timezone: str
    includes_today: bool


class OverviewDay(_ResponseModel):
    date: dt.date
    checkin_count: int
    reject_count: int


class OverviewReportResponse(_ResponseModel):
    """A sorter site's headline counts over a range. `checkin_count` is every
    check-in the site's sorter processed -- processing events, not distinct
    items -- and `home_count + transit_count + other_count` is exactly that
    count, split by where the sorter sent them. `active_days` is the number
    of dates in the range with at least one check-in. `reject_count` counts
    rejects, which are not a subset of check-ins. `days` has one entry per
    date in the range, in order; its counts add up to the two totals."""

    range: ReportRange
    checkin_count: int
    active_days: int
    home_count: int
    transit_count: int
    other_count: int
    reject_count: int
    days: list[OverviewDay]


class VolumeDay(_ResponseModel):
    date: dt.date
    checkin_count: int


class VolumeHour(_ResponseModel):
    """`hour` is what the local clock read, 0 to 23. `checkin_count` is the
    TOTAL for that hour across every day of the range, not an average."""

    hour: int
    checkin_count: int


class VolumeReportResponse(_ResponseModel):
    """A sorter site's check-ins over a range, by day and by wall-clock hour.
    `days` has one entry per date in the range, in order. `hours` always has
    24 entries, for hours 0 to 23 in order -- on every range, including one
    that contains a day the clocks changed on. Each list adds up to
    `checkin_count`."""

    range: ReportRange
    checkin_count: int
    days: list[VolumeDay]
    hours: list[VolumeHour]


class RoutingDay(_ResponseModel):
    """One date's check-ins by destination. `transit_counts` lines up with
    the report's `transit` list: the same length, the same order, entry N
    being the count for `transit[N]`. `home_count`, `transit_counts` and
    `other_count` add up to `checkin_count`."""

    date: dt.date
    checkin_count: int
    home_count: int
    transit_counts: list[int]
    other_count: int


class RoutingReportResponse(_ResponseModel):
    """A sorter site's check-ins over a range by where the sorter routed
    them: CheckinsByDestinationResponse's shape for a range, with the same
    meaning for every field, plus the same split for each date.

    `transit` has one entry for each destination enabled in the site's
    settings, in configured order, zero where there were none. A destination
    is a routing outcome of this sorter: its `key` identifies it within this
    report and says nothing about whether a branch, a site or another sorter
    exists by that name."""

    range: ReportRange
    checkin_count: int
    home: RoutingHome
    transit: list[RoutingDestination]
    transit_count: int
    other_count: int
    days: list[RoutingDay]


class BinVolumeBin(_ResponseModel):
    """One sort bin that check-ins of the range were logged in. `key` is the
    bin's number as the sorter logs it, without leading zeros: an identifier
    of a physical bin, not a count and not a destination. `hours` always has
    24 entries, for hours 0 to 23 of the local clock in order, each the TOTAL
    for that hour across every day of the range; they add up to
    `checkin_count`."""

    key: str
    checkin_count: int
    hours: list[int]


class BinVolumeReportResponse(_ResponseModel):
    """A sorter site's check-ins over a range by the sort bin each was
    logged in. `checkin_count` is every check-in of the range -- the same
    count the overview and volume reports give -- and is exactly
    `known_bin_count + unknown_bin_count`.

    `bins` lists only bins that were OBSERVED in the range, in numeric order,
    and adds up to `known_bin_count`. A bin with no check-ins in the range is
    not listed, because which bins a sorter has is not known: a missing bin
    is not a zero. `unknown_bin_count` is the check-ins whose logged bin was
    missing or was not a bin number.

    A bin says where an item physically went on the sorter. It says nothing
    about how full the bin was, where the item was routed or what it was."""

    range: ReportRange
    checkin_count: int
    known_bin_count: int
    unknown_bin_count: int
    bins: list[BinVolumeBin]


class ReliabilityReason(_ResponseModel):
    reason: RejectReason
    reject_count: int


class ReliabilityDay(_ResponseModel):
    date: dt.date
    checkin_count: int
    reject_count: int


class ReliabilityReportResponse(_ResponseModel):
    """A sorter site's rejects over a range. `reasons` always has the eight
    public reason codes (services.reject_reason) in their fixed order, zero
    where there were none, and adds up to `reject_count`: a code, never a
    stored message. `days` has one entry per date in the range, in order,
    with that date's check-ins beside its rejects; its two columns add up to
    `checkin_count` and `reject_count`."""

    range: ReportRange
    checkin_count: int
    reject_count: int
    reasons: list[ReliabilityReason]
    days: list[ReliabilityDay]
