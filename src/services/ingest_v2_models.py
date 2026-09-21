"""Privacy Contract v2: the request models (docs/contract-v2-design.md).

Every model here is an explicit, typed allowlist: `extra="forbid"` (an unexpected field -- including `customer_id`,
`branch_id`, `patron_id`, `barcode`, `title`, `raw_message` -- is a validation error, never silently dropped), `strict`
(no `"true"` for a boolean, no `1` for a string), and `frozen`. There is no free-text field and no open dictionary: every
string is a fixed-format identifier or a member of a closed enum.

Tenant scope is NOT in a payload. The server derives `customer_id` and `branch_id` from the authenticated agent token.

Standard library and pydantic only, so it imports the same way as `src.services...` (main.py) and `services...`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

CONTRACT_VERSION = 2

# Bounds. One request carries at most MAX_EVENTS_TOTAL events across ALL three lists.
MAX_EVENTS_TOTAL = 1000
MAX_EVENTS_PER_LIST = 1000

# Formats. Kept as plain strings so the Alembic migration and the API state the SAME rule (the tests compare them).
UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
HMAC_HEX_PATTERN = r"^[0-9a-f]{64}$"
DESTINATION_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
BIN_PATTERN = r"^[a-z0-9][a-z0-9_]{0,15}$"
ERROR_CLASS_PATTERN = r"^[a-z][a-z_]{0,31}$"  # the database's rule; the closed enum below is the API's

ERROR_CLASSES = (
    "item_not_found",
    "ils_acs_failure",
    "rfid_collision",
    "configuration_error",
    "routing_error",
    "communication_error",
    "other",
    "unknown",
)
ErrorClass = Literal[
    "item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error",
    "routing_error", "communication_error", "other", "unknown",
]

# The collector's OVERALL state. A kind of failure (`auth_failure` included) is a `last_error_class`, never a status.
HEALTH_STATUSES = ("healthy", "degraded", "error")
HealthStatus = Literal["healthy", "degraded", "error"]

LAST_ERROR_CLASSES = (
    "retryable_infra", "auth_failure", "permanent_rejection", "source_unavailable", "configuration_error", "other",
)
LastErrorClass = Literal[
    "retryable_infra", "auth_failure", "permanent_rejection", "source_unavailable", "configuration_error", "other",
]

# The derived state of one ACS item record (message code 10). It is NOT the raw message code, which never leaves the collector:
#   hold          a 101 record that is hold-positive (`101YNY`)
#   non_hold_101  a 101 record that is not a hold
#   other_code10  any other code-10 record: ignored by Overview, but it takes part in Live Today's latest-record-wins rule
# (Message-64 patron records are never events at all.)
ACS_ITEM_STATES = ("hold", "non_hold_101", "other_code10")
NON_HOLD_STATES = ("non_hold_101", "other_code10")

MAX_COUNTER = 10_000_000

# --- timestamps: ISO-8601 WITH an offset, and nothing else -------------------------------------------------------

_ISO_WITH_OFFSET = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
_FUTURE_TOLERANCE = timedelta(days=1)


def _parse_offset_timestamp(value: object) -> datetime:
    """A string in the exact form `YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+hh:mm)`, returned as an aware UTC datetime. A naive
    timestamp, an epoch number, a date, a datetime object or any other spelling is rejected; so is an instant before 2000
    or more than a day ahead of now. Only fixed messages are raised: the value is never part of an error."""
    if not isinstance(value, str) or _ISO_WITH_OFFSET.fullmatch(value) is None:
        raise PydanticCustomError("timestamp_format", "Timestamp must be ISO-8601 with a UTC offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise PydanticCustomError("timestamp_format", "Timestamp must be ISO-8601 with a UTC offset") from None
    if parsed.tzinfo is None:  # unreachable behind the pattern; kept because "never naive" is the rule
        raise PydanticCustomError("timestamp_format", "Timestamp must be ISO-8601 with a UTC offset")
    instant = parsed.astimezone(UTC)
    if not _EARLIEST <= instant <= datetime.now(UTC) + _FUTURE_TOLERANCE:
        raise PydanticCustomError("timestamp_range", "Timestamp is outside the accepted range")
    return instant


AwareTimestamp = Annotated[datetime, BeforeValidator(_parse_offset_timestamp)]


def _exact_int(value: object) -> object:
    """`Literal[2]` alone would accept the float 2.0 (2.0 == 2 in Python) and the JSON `true` is filtered by strict mode only
    for some types; the contract says the integer 2, so anything whose type is not exactly `int` is refused here."""
    if type(value) is not int:
        raise PydanticCustomError("literal_error", "Input is not one of the allowed values")
    return value


ContractVersion = Annotated[Literal[2], BeforeValidator(_exact_int)]

# --- identifiers and slugs ---------------------------------------------------------------------------------------

KeyId = Annotated[str, StringConstraints(pattern=UUID4_PATTERN)]
RulesetId = Annotated[str, StringConstraints(pattern=UUID4_PATTERN)]
HmacKey = Annotated[str, StringConstraints(pattern=HMAC_HEX_PATTERN)]
Destination = Annotated[str, StringConstraints(pattern=DESTINATION_PATTERN)]
Bin = Annotated[str, StringConstraints(pattern=BIN_PATTERN)]
Counter = Annotated[int, Field(ge=0, le=MAX_COUNTER)]


class _V2Model(BaseModel):
    """The base of every v2 model. Every subclass inherits the three settings that make it an allowlist; a test walks all
    subclasses to prove none overrides them."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# --- events ------------------------------------------------------------------------------------------------------

class CheckinEvent(_V2Model):
    event_key: HmacKey
    event_time: AwareTimestamp
    item_key: HmacKey | None = None
    destination: Destination
    bin: Bin


class RejectEvent(_V2Model):
    event_key: HmacKey
    event_time: AwareTimestamp
    error_class: ErrorClass
    item_key: HmacKey | None = None


class AcsHoldEvent(_V2Model):
    """An ACS item record that is hold-positive (`101YNY`). It carries the collector-derived classification; nothing else about
    the record (no barcode, patron, title, raw message or raw message code) is here."""

    state: Literal["hold"]
    event_key: HmacKey
    event_time: AwareTimestamp
    item_key: HmacKey
    destination: Destination
    is_ill: bool
    is_branch_services: bool
    is_collection_services: bool
    ruleset_id: RulesetId | None = None


class AcsNonHoldEvent(_V2Model):
    """An ACS item record that is NOT a hold. It exists only so a later record can retract an earlier hold (the dashboard's
    latest-record-wins rule), so it carries exactly what retraction needs. There is deliberately no destination, no
    classification flag and no ruleset here -- and `extra="forbid"` means none can be sent, so no dummy value is ever
    invented to fit the hold shape."""

    state: Literal["non_hold_101", "other_code10"]
    event_key: HmacKey
    event_time: AwareTimestamp
    item_key: HmacKey


AcsItemEvent = Annotated[AcsHoldEvent | AcsNonHoldEvent, Field(discriminator="state")]


class UploadV2Request(_V2Model):
    contract_version: ContractVersion
    key_id: KeyId
    checkins: list[CheckinEvent] = Field(default_factory=list, max_length=MAX_EVENTS_PER_LIST)
    rejects: list[RejectEvent] = Field(default_factory=list, max_length=MAX_EVENTS_PER_LIST)
    acs_items: list[AcsItemEvent] = Field(default_factory=list, max_length=MAX_EVENTS_PER_LIST)

    @model_validator(mode="after")
    def _total_is_bounded(self) -> UploadV2Request:
        if len(self.checkins) + len(self.rejects) + len(self.acs_items) > MAX_EVENTS_TOTAL:
            raise PydanticCustomError("too_many_events", "Too many events in one request")
        return self


# --- heartbeat ---------------------------------------------------------------------------------------------------

class StatusV2Request(_V2Model):
    """The whole v2 heartbeat. Typed and allowlisted: no free-text `last_error`, no open dictionary, no exception string, no
    HTTP/response preview. Each heartbeat is a FULL snapshot of these fields (an omitted optional one is stored as NULL)."""

    contract_version: ContractVersion
    key_id: KeyId
    status: HealthStatus
    last_error_class: LastErrorClass | None = None
    pending_outbox_count: Counter | None = None
    quarantined_count: Counter | None = None
    oldest_pending_event_at: AwareTimestamp | None = None
    last_success_at: AwareTimestamp | None = None
    watcher_last_active_at: AwareTimestamp | None = None
