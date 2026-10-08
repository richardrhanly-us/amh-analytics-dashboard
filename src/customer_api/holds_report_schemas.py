"""The response model for the customer API's holds report.

Two integer counts and the range they cover -- nothing else. The rows they are worked out from carry patron ids, raw
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

    They are totals for the whole range: each counts an item's latest record in the range, so a range's figure is not
    the sum of its days' figures, and no per-day figure is given. Which destination an interlibrary loan went to is
    not given either."""

    model_config = ConfigDict(extra="forbid")

    range: ReportRange
    public_hold_count: int
    ill_hold_count: int
