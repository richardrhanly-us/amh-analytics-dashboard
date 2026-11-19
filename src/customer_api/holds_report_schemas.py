"""The response model for the customer API's holds report.

Two integer counts, how many days had data, and the range they cover -- nothing else. The rows they are worked out from carry patron ids, raw
messages and, for the legacy era, the library's lists of its own service accounts; none of it, and none of the other
kinds of hold the classifiers count, has a field here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from customer_api.report_schemas import ReportRange


class HoldsReportResponse(BaseModel):
    """A sorter site's holds over a range.

    `public_hold_count` is the holds for library patrons. `ill_hold_count` is the interlibrary loan holds. A hold for
    one of the library's own service accounts, or for an interlibrary loan, is not a public hold; how many holds there
    are of any other kind is not given, so the two numbers do not add up to all holds.

    They are SUMS OF DAILY COUNTS (UI2 v7): each day counts an item at most once, by its latest record that day, and the
    days are added -- so over several days they are summed daily hold classifications, not distinct holds or items. A
    one-day range is that day's count. `days_with_data` is how many of the range's days had any record to count: fewer
    than `range.days` means part of the range has no data, not that nothing happened. Which destination an interlibrary
    loan went to is not given.

    Both counts are CONFIRMED holds only (docs/hold-count-accuracy.md). Three more integers say what they leave out,
    each never folded into a count:

    `public_holds_partial_days` -- days whose public count lacks an UNAVAILABLE part: before this sorter's collector
    was corrected, holds for pickup at Westside could not be told from items in transit, so on those days they are not
    counted. The missing part is unknown, not zero.
    `ill_uncertain_days` -- days, before the collector was corrected, whose ILL count may miss or add a few holds.
    `unknown_alert_count` -- item-days whose latest record had an alert type that is not recognised: counted in
    neither number."""

    model_config = ConfigDict(extra="forbid")

    range: ReportRange
    public_hold_count: int
    ill_hold_count: int
    days_with_data: int
    public_holds_partial_days: int
    ill_uncertain_days: int
    unknown_alert_count: int
