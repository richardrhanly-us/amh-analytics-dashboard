"""Response models for the customer API's operational read routes.

Every model lists exactly the fields a browser may see. The rows these are
built from sit next to identifiers that never leave the server -- the ingest
key's id and algorithm, the row's database id, the operational customer_id
and branch_id -- and none of them has a field here.
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from services.reject_reason import RejectReason


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestStatusFields(_ResponseModel):
    """A branch's latest Contract v2 collector heartbeat. Timestamps are
    datetimes here and are serialized as ISO-8601."""

    health_status: str | None
    last_error_class: str | None
    pending_outbox_count: int | None
    quarantined_count: int | None
    oldest_pending_event_at: datetime | None
    last_success_at: datetime | None
    watcher_last_active_at: datetime | None
    last_heartbeat_at: datetime | None
    collector_last_run_at: datetime | None
    collector_next_run_at: datetime | None
    collector_run_duration_ms: int | None
    collector_schedule_status: str | None


class IngestStatusResponse(_ResponseModel):
    """`status` is null for a branch with no active Contract v2 ingest key:
    a valid answer ("nothing to report"), not an error."""

    status: IngestStatusFields | None


class CheckinCountResponse(_ResponseModel):
    """Check-ins on one local calendar day. `date` is the day that was asked
    for and `timezone` the IANA zone it was interpreted in. How the count was
    assembled from the branch's legacy and current data is not part of the
    answer."""

    date: dt.date
    timezone: str
    checkin_count: int


class CheckinHourCount(_ResponseModel):
    """Check-ins in one wall-clock hour: `hour` is what the local clock read,
    0 to 23."""

    hour: int
    checkin_count: int


class CheckinsByHourResponse(_ResponseModel):
    """Check-ins on one local calendar day, by wall-clock hour. `date` and
    `timezone` are as in CheckinCountResponse. `hours` always has 24 entries,
    for hours 0 to 23 in order, zero where nothing happened -- on every date,
    including the two a year whose local day is not 24 hours long. The counts
    add up to that day's CheckinCountResponse.checkin_count."""

    date: dt.date
    timezone: str
    hours: list[CheckinHourCount]


class RejectCountResponse(_ResponseModel):
    """Rejects on one local calendar day. `date` and `timezone` are as in
    CheckinCountResponse. Every stored reject of the day is counted, whatever
    its reason; neither the reasons nor how the count was assembled from the
    branch's legacy and current data is part of the answer."""

    date: dt.date
    timezone: str
    reject_count: int


class RejectReasonCount(_ResponseModel):
    """Rejects with one reason. `reason` is one of the eight public reason
    codes (services.reject_reason): a code, never display text, and never a
    stored message or a stored class that is not one of the eight."""

    reason: RejectReason
    reject_count: int


class RejectsByReasonResponse(_ResponseModel):
    """Rejects on one local calendar day, by reason. `date` and `timezone` are
    as in CheckinCountResponse. `reasons` always has eight entries, one for
    each reason code in its fixed order, zero where there were none. The
    counts add up to that day's RejectCountResponse.reject_count, which is
    why no total is repeated here."""

    date: dt.date
    timezone: str
    reasons: list[RejectReasonCount]
